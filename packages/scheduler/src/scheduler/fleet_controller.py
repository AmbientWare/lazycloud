from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from compute.capacity_errors import CapacityReservationLockContendedError
from compute.projection import PrivateUnitState
from compute.release_rollout import ComputeReleaseRolloutService
from compute.state import RedisComputeStateRepository
from control.releases import DeploymentReleaseService
from coordination.token_lock import release_token_lock, try_acquire_token_lock
from database.records.apps import AppRecord
from identity.auth import AuthService
from identity.device_auth import DeviceAuthorizationService
from observability.usage import WorkerEventService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.scheduling import SchedulerWorkerStatus, WorkerRemovalResult
from shared.step_timings import StepTimings
from shared.timestamps import utc_now

from scheduler.agent_pool import (
    AgentPoolConfig,
    AgentPoolReconcileResult,
    SchedulerAgentPoolService,
)
from scheduler.autoscaling import (
    CONTAINER_DELIVERY_DEADLINE_SECONDS,
    CONTAINER_START_DEADLINE_SECONDS,
)
from scheduler.capacity_reservations import (
    CapacityProvisioningReservation,
)
from scheduler.containers import (
    SchedulerContainerRequestService,
)
from scheduler.fleet import WorkerPoolStateSnapshot
from scheduler.pool_drain import (
    WorkerPoolDrainResult,
    WorkerPoolDrainService,
)
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import (
    WorkerPreemptionResult,
)
from scheduler.reconciliation import (
    _NO_BILLING_ENFORCEMENT,
    _NO_BILLING_RECONCILIATION,
    _NO_METER_EVENT_SWEEP,
    _NO_PLAN_CHANGES,
    BILLING_ENFORCEMENT_LOCK_TTL_SECONDS,
    CAPACITY_PASS_SLOW_SECONDS,
    CONTAINER_EXPIRY_LOCK_TTL_SECONDS,
    LOGGER,
    MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS,
    ORPHANED_CONTAINER_CONFIRMATION_SECONDS,
    ORPHANED_CONTAINER_FAILURE_REASON,
    ORPHANED_CONTAINER_RECONCILE_INTERVAL_SECONDS,
    SchedulerBillingEnforcementBatch,
    SchedulerBillingReconciliationBatch,
    SchedulerCapacityControls,
    SchedulerMaintenanceControls,
    SchedulerMeterEventBatch,
    SchedulerMeterOutboxService,
    SchedulerPlanChangeBatch,
    SchedulerRunResult,
    SchedulerStateStores,
    SchedulerWorkloadControls,
    WorkerCleanupRepository,
    _log_worker_pool_drain_decisions,
    _merge_run_results,
    _MeterEventSweep,
    _record_worker_pool_drain_observability,
)
from scheduler.reserves import reserve_free_capacity
from scheduler.services import FleetServices


@dataclass
class FleetController:
    services: FleetServices | None = None
    workloads: SchedulerWorkloadControls = field(default_factory=SchedulerWorkloadControls)
    states: SchedulerStateStores = field(default_factory=SchedulerStateStores)
    capacity: SchedulerCapacityControls = field(default_factory=SchedulerCapacityControls)
    maintenance: SchedulerMaintenanceControls = field(default_factory=SchedulerMaintenanceControls)
    managed_compute_reconcile_interval_seconds: float = MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
    last_managed_compute_reconcile_at: datetime | None = field(default=None, init=False)
    last_release_reconcile_at: datetime | None = field(default=None, init=False)
    last_release_generation: int = field(default=0, init=False)
    last_release_worker_fingerprint: str = field(default="", init=False)
    worker_pool_drain_interval_seconds: float = 30.0
    last_worker_pool_drain_at: datetime | None = field(default=None, init=False)
    custom_domain_reconcile_interval_seconds: float = 60.0
    last_custom_domain_reconcile_at: datetime | None = field(default=None, init=False)
    token_prune_interval_seconds: float = 3600.0
    last_token_prune_at: datetime | None = field(default=None, init=False)
    retention_interval_seconds: float = 3600.0
    retention_retry_initial_seconds: float = 30.0
    retention_retry_max_seconds: float = 900.0
    last_retention_at: datetime | None = field(default=None, init=False)
    next_retention_attempt_at: datetime | None = field(default=None, init=False)
    retention_consecutive_failures: int = field(default=0, init=False)
    email_prune_interval_seconds: float = 3600.0
    last_email_prune_at: datetime | None = field(default=None, init=False)
    event_prune_interval_seconds: float = 3600.0
    last_event_prune_at: datetime | None = field(default=None, init=False)
    last_reported_abandoned_meter_events: int | None = field(default=None, init=False)
    """The outstanding abandoned figure the last line reported.

    The drain's own counts are a delta and say nothing on an idle tick, but the
    backlog they leave behind is a standing total that has to stay visible. This
    is what keeps a 1 Hz loop from printing the same number every second while
    still printing it whenever it moves."""

    billing_reconcile_interval_seconds: float = 3600.0
    last_billing_reconcile_at: datetime | None = field(default=None, init=False)
    billing_enforcement_interval_seconds: float = 5.0
    """How often compute nobody can be billed for is stopped.

    Seconds, not the hour reconciliation runs on, because this interval is money:
    every second between an account running out and its containers stopping is
    spend that will not be collected. Five rather than every tick because the
    lag it adds is small beside the interval usage takes to reach the ledger at
    all, and a page query per second per replica buys nothing against that."""

    last_billing_enforcement_at: datetime | None = field(default=None, init=False)
    worker_pool_drain_logged_at: dict[str, datetime] = field(default_factory=dict)
    """When each pool's drain decision was last written to the log.

    The event beside it is emitted only when the decision changes, which says
    nothing at all about a pool that has been stuck on one answer for an hour --
    exactly the case an operator is looking at when they ask why nothing scaled
    down. This is the heartbeat that makes a standing reason readable.
    """

    worker_pool_drain_event_signatures: dict[str, tuple[str, ...]] = field(
        default_factory=dict,
        init=False,
    )
    orphaned_container_reconcile_interval_seconds: float = (
        ORPHANED_CONTAINER_RECONCILE_INTERVAL_SECONDS
    )
    orphaned_container_confirmation_seconds: float = ORPHANED_CONTAINER_CONFIRMATION_SECONDS
    last_orphaned_container_reconcile_at: datetime | None = field(default=None, init=False)

    @property
    def runtime_services(self) -> FleetServices:
        if self.services is None:
            msg = "scheduler backend services are required for this operation"
            raise RuntimeError(msg)
        return self.services

    @property
    def container_scheduler(self) -> SchedulerContainerRequestService:
        container_requests = self.workloads.containers
        if container_requests is None:
            msg = "scheduler container request service was not injected"
            raise RuntimeError(msg)
        return container_requests

    @property
    def compute_states(self) -> RedisComputeStateRepository:
        compute_state = self.states.compute
        if compute_state is None:
            msg = "scheduler compute state repository was not injected"
            raise RuntimeError(msg)
        return compute_state

    @property
    def agent_pool_service(self) -> SchedulerAgentPoolService:
        agent_pools = self.capacity.agent_pools
        if agent_pools is None:
            msg = "scheduler agent pool service was not injected"
            raise RuntimeError(msg)
        return agent_pools

    @property
    def pool_states(self) -> SchedulerPoolStateService:
        pools = self.states.pools
        if pools is None:
            msg = "scheduler pool state service was not injected"
            raise RuntimeError(msg)
        return pools

    @property
    def worker_pool_drain_service(self) -> WorkerPoolDrainService:
        service = self.capacity.worker_pool_drain
        if service is None:
            msg = "scheduler worker pool drain service was not injected"
            raise RuntimeError(msg)
        return service

    def reconcile_agent_pools(
        self,
        *,
        now: datetime | None = None,
    ) -> list[AgentPoolReconcileResult]:
        configs = self._agent_pool_configs()
        if not configs:
            return []
        return self.agent_pool_service.reconcile(configs, now=now)

    def refresh_pool_states(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, WorkerPoolStateSnapshot]:
        return self.pool_states.refresh(
            agent_pool_configs=self._agent_pool_configs(),
            now=now,
        )

    def reconcile_app_lifecycle(self, *, limit: int = 25) -> list[AppRecord]:
        return self.runtime_services.apps.reconcile_pending(limit=limit)

    def drain_worker_pools(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[WorkerPoolDrainResult]:
        results = self.worker_pool_drain_service.reconcile(now=now, limit=limit)
        _record_worker_pool_drain_observability(
            self.runtime_services,
            results,
            event_signatures=self.worker_pool_drain_event_signatures,
        )
        _log_worker_pool_drain_decisions(
            results,
            logged_at=self.worker_pool_drain_logged_at,
            now=now or utc_now(),
        )
        return results

    def run_acquisition_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
        retry_container_ids: Sequence[str] = (),
    ) -> SchedulerRunResult:
        """Turn recorded capacity demand into a machine, and nothing else.

        A request that found no worker waits here for a reserve to resume or a
        machine to be bought. The pass runs on its own wake and makes no
        provider inventory reads, so that request never waits behind them.

        Given `retry_container_ids`, it retries only that demand and skips the
        recovery scan and the due-demand query.
        """

        if not include_containers:
            return SchedulerRunResult()
        timings = StepTimings()
        if not retry_container_ids:
            with timings.step("recovery"):
                try:
                    self.runtime_services.compute.reconcile_capacity_recovery(
                        now=now or utc_now(),
                        limit=container_limit,
                    )
                except Exception:
                    LOGGER.exception("scheduler capacity recovery failed")
        with timings.step("acquire"):
            # `now` stays None in the running process so each request's retry is
            # timed from its own attempt, not from the start of the pass.
            sweep = self.container_scheduler.acquire_capacity(
                now=now,
                limit=container_limit,
                container_ids=retry_container_ids or None,
            )
        if sweep.acquired or sweep.contended:
            timings.log(
                LOGGER,
                "scheduler acquisition pass: %d request(s) served, %d contended",
                sweep.acquired,
                len(sweep.contended),
            )
        return SchedulerRunResult(capacity_demand_contended=list(sweep.contended))

    def run_capacity_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
    ) -> SchedulerRunResult:
        """Keep the fleet and its records agreeing with each other.

        Slower than placement and faster than housekeeping, because the work
        here decides what capacity exists rather than what runs on it. Billing
        enforcement sits in this pass rather than with the rest of billing: it
        touches only Postgres, and its interval is denominated in money.

        """

        timings = StepTimings()
        with timings.step("billing_enforcement"):
            billing_enforcement = self._best_effort_enforce_billing(now=now)
        if not include_containers:
            return SchedulerRunResult(
                billing_enforcement_unfunded_count=billing_enforcement.unfunded_count,
                billing_enforcement_stopped_count=billing_enforcement.stopped_count,
                billing_enforcement_failure_count=billing_enforcement.failed_count,
            )
        with timings.step("app_lifecycle"):
            app_lifecycle = self._best_effort_reconcile_app_lifecycle(limit=container_limit)
        with timings.step("capacity_interruptions"):
            interruptions = self._best_effort_reconcile_capacity_interruptions(now=now)
        with timings.step("expire_containers"):
            expired = self._best_effort_expire_containers(now=now)
        with timings.step("worker_cleanup"):
            worker_cleanups = self._best_effort_cleanup_workers(now=now)
        with timings.step("preemptions"):
            preemptions = self._best_effort_recover_unsettled_preemptions(limit=container_limit)
        with timings.step("agent_pools"):
            agent_pools = self._best_effort_reconcile_agent_pools(now=now)
        with timings.step("reserves"):
            self._best_effort_plan_reserves(now=now)
        with timings.step("managed_compute"):
            managed_compute = self._best_effort_reconcile_managed_compute(now=now)
        with timings.step("release_rollout"):
            self._best_effort_reconcile_release(now=now)
        with timings.step("pool_states"):
            pool_states = self._best_effort_refresh_pool_states(now=now)
        with timings.step("capacity_reservations"):
            reservations = self._best_effort_reconcile_capacity_reservations(now=now)
        with timings.step("orphaned_containers"):
            orphaned = self._best_effort_reconcile_orphaned_containers(now=now)
        with timings.step("worker_pool_drains"):
            drains = self._best_effort_drain_worker_pools(now=now, limit=container_limit)
        if timings.total_seconds() >= CAPACITY_PASS_SLOW_SECONDS:
            timings.log(LOGGER, "scheduler capacity pass was slow")
        return SchedulerRunResult(
            app_lifecycle_reconciliations=app_lifecycle,
            capacity_interruptions=interruptions,
            expired_containers=expired,
            worker_cleanups=worker_cleanups,
            settled_preemptions=preemptions,
            agent_pool_reconciliations=agent_pools,
            managed_compute_reconciliations=managed_compute,
            pool_states=pool_states,
            capacity_reservations=reservations,
            orphaned_containers_failed=orphaned,
            worker_pool_drains=drains,
            billing_enforcement_unfunded_count=billing_enforcement.unfunded_count,
            billing_enforcement_stopped_count=billing_enforcement.stopped_count,
            billing_enforcement_failure_count=billing_enforcement.failed_count,
        )

    def run_housekeeping_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
    ) -> SchedulerRunResult:
        """Everything that talks to somebody else's service.

        Stripe, S3, and Cloudflare answer on their own schedule. Nothing placement needs is
        produced here: the allowance admission reads is written when usage is
        priced, so draining the outbox afterwards is downstream of the number
        that decides whether work may start.
        """

        try:
            self.runtime_services.deployment_plans.reconcile_pending(limit=container_limit)
        except Exception:
            LOGGER.exception("scheduler deployment prune recovery failed")
        access_observed = None
        access_failures = 0
        if self.maintenance.storage_access is not None:
            try:
                access_observed = self.maintenance.storage_access.reconcile()
            except Exception:
                access_failures = 1
                LOGGER.exception("storage access ingestion failed; delivery remains unacknowledged")
        if self.maintenance.volume_deletion is not None:
            self.maintenance.volume_deletion.reconcile_due(now=now, limit=container_limit)
        if self.maintenance.disk_deletion is not None:
            self.maintenance.disk_deletion.reconcile_due(now=now, limit=container_limit)
        if self.maintenance.disk_volumes is not None:
            self.maintenance.disk_volumes.reconcile_due(now=now, limit=container_limit)
        volume_metering_count, volume_metering_failure_count = self._meter_persistent_volumes(
            now=now,
            limit=container_limit,
        )
        meter_events = self._drain_meter_events(now=now)
        if self.maintenance.billing_payments is not None:
            try:
                self.maintenance.billing_payments.maintain(now=now)
            except Exception:
                LOGGER.exception("prepaid payment maintenance failed; purchases remain recorded")
        plan_changes = self._settle_plan_changes(now=now)
        billing_reconciliation = self._best_effort_reconcile_billing(now=now)
        self._best_effort_deliver_email(now=now)
        self._best_effort_reconcile_custom_domains(now=now)
        expired_tokens_pruned = (
            self._best_effort_prune_expired_tokens(now=now) if include_containers else 0
        )
        events_pruned = self._best_effort_prune_events(now=now) if include_containers else 0
        objects_removed, retention_failure_count = (
            self._best_effort_retain_artifacts(now=now) if include_containers else (0, 0)
        )
        return SchedulerRunResult(
            expired_tokens_pruned=expired_tokens_pruned,
            storage_access_observed=access_observed,
            storage_access_failures=access_failures,
            events_pruned=events_pruned,
            volume_metering_count=volume_metering_count,
            volume_metering_failure_count=volume_metering_failure_count,
            meter_events_sent_count=meter_events.sent_count,
            meter_events_retried_count=meter_events.retried_count,
            meter_events_abandoned_count=meter_events.abandoned_count,
            meter_events_abandoned_outstanding_count=meter_events.abandoned_outstanding_count,
            meter_events_abandoned_outstanding_nanos=meter_events.abandoned_outstanding_nanos,
            plan_changes_applied_count=plan_changes.applied_count,
            plan_changes_not_applied_count=plan_changes.not_applied_count,
            plan_changes_retried_count=plan_changes.retried_count,
            plan_changes_abandoned_count=plan_changes.abandoned_count,
            plan_changes_open_count=plan_changes.open_count,
            billing_reconcile_checked_count=billing_reconciliation.accounts_checked,
            billing_reconcile_divergent_count=billing_reconciliation.divergent_count,
            billing_reconcile_failure_count=billing_reconciliation.unreachable_count,
            objects_removed=objects_removed,
            retention_failure_count=retention_failure_count,
        )

    def _best_effort_reconcile_app_lifecycle(self, *, limit: int) -> list[AppRecord]:
        try:
            return self.reconcile_app_lifecycle(limit=limit)
        except Exception:
            LOGGER.exception("scheduler app lifecycle reconciliation failed")
            return []

    def _meter_persistent_volumes(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> tuple[int, int]:
        volume_metering = self.maintenance.volume_metering
        if volume_metering is None:
            return 0, 0
        try:
            result = volume_metering.reconcile_due(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler persistent-volume metering failed")
            return 0, 1
        return result.metered_count, result.failure_count

    def _drain_meter_events(self, *, now: datetime | None) -> _MeterEventSweep:
        """Hand the payment provider the events the pricer already queued.

        The sweep settles each row on its own, so a provider outage — or a
        credential this process cannot read — shows up here as a rising retry
        count against a queryable backlog and a line every tick, never as a lost
        charge and never as silence.

        The backlog of charges given up on is read beside the deltas and read
        even when the sending failed, because a drain that cannot reach the
        provider is when that figure matters most. It is also the one figure
        that survives the tick that produced it: a row is abandoned once, is
        never pruned, and stands there as metered usage nobody was billed for
        until an operator has answered for it.
        """

        meter_outbox = self.maintenance.meter_outbox
        if meter_outbox is None:
            return _NO_METER_EVENT_SWEEP
        try:
            drained: SchedulerMeterEventBatch = meter_outbox.drain(now=now)
        except Exception:
            LOGGER.exception("scheduler meter event delivery failed")
            drained = _NO_METER_EVENT_SWEEP
        backlog = self._abandoned_meter_events(meter_outbox)
        if backlog is None:
            # Said once, by the exception that could not read it. A summary line
            # here would put a figure of zero beside the failure to read one.
            return _MeterEventSweep(
                sent_count=drained.sent_count,
                retried_count=drained.retried_count,
                abandoned_count=drained.abandoned_count,
            )
        outstanding_count, outstanding_nanos = backlog
        moved = (drained.sent_count, drained.retried_count, drained.abandoned_count)
        if any(moved) or outstanding_count != self.last_reported_abandoned_meter_events:
            # Only when a tick did something, or when the standing backlog is not
            # the number last reported. The loop runs about once a second and an
            # idle outbox is the ordinary case, so a line per tick would bury the
            # one that says a charge was refused.
            LOGGER.log(
                logging.WARNING if outstanding_count else logging.INFO,
                "scheduler: meter events sent=%d retried=%d abandoned=%d; "
                "%d rows metering %d nanodollars never reached the provider",
                *moved,
                outstanding_count,
                outstanding_nanos,
            )
            self.last_reported_abandoned_meter_events = outstanding_count
        return _MeterEventSweep(
            sent_count=drained.sent_count,
            retried_count=drained.retried_count,
            abandoned_count=drained.abandoned_count,
            abandoned_outstanding_count=outstanding_count,
            abandoned_outstanding_nanos=outstanding_nanos,
        )

    def _abandoned_meter_events(
        self, meter_outbox: SchedulerMeterOutboxService
    ) -> tuple[int, int] | None:
        """How many charges stand given up on and what they metered, or nothing."""

        try:
            backlog = meter_outbox.abandoned_backlog()
        except Exception:
            LOGGER.exception("scheduler abandoned meter event backlog could not be read")
            # Forgotten rather than carried, so the next reading is reported
            # whatever it says instead of being compared against a number this
            # tick never had and passed over as unchanged.
            self.last_reported_abandoned_meter_events = None
            return None
        return backlog.count, backlog.value_nanos

    def _settle_plan_changes(self, *, now: datetime | None) -> SchedulerPlanChangeBatch:
        """Finish the plan changes whose outcome nobody recorded.

        Every tick, because an intent with no outcome is a customer who may have
        been charged for a plan this platform is not billing them on, and the
        window it stays open in is the window admission judges them on the wrong
        allowance.
        """

        plan_changes = self.maintenance.plan_changes
        if plan_changes is None:
            return _NO_PLAN_CHANGES
        try:
            result = plan_changes.settle_open(now=now)
        except Exception:
            LOGGER.exception("scheduler plan change settlement failed")
            return _NO_PLAN_CHANGES
        if result.applied_count or result.abandoned_count or result.open_count:
            LOGGER.info(
                "scheduler: plan changes applied=%d not_applied=%d retried=%d abandoned=%d open=%d",
                result.applied_count,
                result.not_applied_count,
                result.retried_count,
                result.abandoned_count,
                result.open_count,
            )
        return result

    def _best_effort_reconcile_billing(
        self, *, now: datetime | None = None
    ) -> SchedulerBillingReconciliationBatch:
        """Compare the provider's record against this platform's, on an interval.

        Hourly rather than per tick: it reads the provider once or twice per
        account, and what it looks for — a delivery that never arrived, a plan
        changed in the provider's dashboard — is not something a second makes a
        difference to.
        """

        reconciliation = self.maintenance.billing_reconciliation
        if reconciliation is None:
            return _NO_BILLING_RECONCILIATION
        current = now or utc_now()
        if (
            self.last_billing_reconcile_at is not None
            and (current - self.last_billing_reconcile_at).total_seconds()
            < self.billing_reconcile_interval_seconds
        ):
            return _NO_BILLING_RECONCILIATION
        try:
            result = reconciliation.reconcile(now=current)
        except Exception:
            LOGGER.exception("scheduler billing reconciliation failed")
            self.last_billing_reconcile_at = current
            return _NO_BILLING_RECONCILIATION
        self.last_billing_reconcile_at = current
        return result

    def _best_effort_enforce_billing(
        self, *, now: datetime | None = None
    ) -> SchedulerBillingEnforcementBatch:
        """Stop compute for accounts nobody can be billed for, on a fast interval.

        Locked, unlike reconciliation beside it, because this one writes: two
        schedulers sweeping together would each stop the same containers and
        publish the same lifecycle change twice. Stopping is idempotent for a
        container already terminal, so the lock is about the wasted pass and the
        duplicate announcement rather than about correctness.

        The stamp is written on both branches so a failing sweep waits its
        interval rather than retrying every tick — which for a database that is
        down would be the loop hammering it.
        """

        enforcement = self.maintenance.billing_enforcement
        if enforcement is None:
            return _NO_BILLING_ENFORCEMENT
        current = now or utc_now()
        if (
            self.last_billing_enforcement_at is not None
            and (current - self.last_billing_enforcement_at).total_seconds()
            < self.billing_enforcement_interval_seconds
        ):
            return _NO_BILLING_ENFORCEMENT
        cron_job_locks = self.states.cron_job_locks
        if cron_job_locks is None:
            return _NO_BILLING_ENFORCEMENT
        lock_key = cron_job_locks.key("scheduler", "leases", "billing-enforcement")
        token = uuid4().hex
        if not try_acquire_token_lock(
            cron_job_locks,
            lock_key,
            token,
            ttl_seconds=BILLING_ENFORCEMENT_LOCK_TTL_SECONDS,
        ):
            return _NO_BILLING_ENFORCEMENT
        try:
            result = enforcement.enforce(now=current)
        except Exception:
            LOGGER.exception("scheduler billing enforcement failed")
            self.last_billing_enforcement_at = current
            return _NO_BILLING_ENFORCEMENT
        finally:
            release_token_lock(cron_job_locks, lock_key, token)
        self.last_billing_enforcement_at = current
        return result

    def _best_effort_deliver_email(self, *, now: datetime | None = None) -> None:
        """Hand the provider the messages requests have already committed to.

        Best effort in the same sense as the rest of this pass: the outbox is
        durable, so a failed sweep delays delivery rather than losing it, and the
        next tick claims the same rows. What is not best effort is the standing
        backlog of abandoned messages, which is logged whenever it is not zero,
        because each one is somebody who was never told something.
        """
        email_outbox = self.maintenance.email_outbox
        if email_outbox is None:
            return
        try:
            drained = email_outbox.drain(now=now)
        except Exception:
            LOGGER.exception("scheduler email delivery failed")
            return
        if drained.abandoned_count:
            LOGGER.error(
                "scheduler abandoned %d email messages this sweep",
                drained.abandoned_count,
            )
        try:
            backlog = email_outbox.abandoned_backlog()
        except Exception:
            LOGGER.exception("scheduler could not read the abandoned email backlog")
            return
        if backlog:
            LOGGER.error("%d email messages have been given up on", backlog)
        current = now or utc_now()
        if (
            self.last_email_prune_at is not None
            and (current - self.last_email_prune_at).total_seconds()
            < self.email_prune_interval_seconds
        ):
            return
        try:
            email_outbox.redact(now=current)
        except Exception:
            LOGGER.exception("scheduler email redaction failed")
            return
        self.last_email_prune_at = current

    def _best_effort_retain_artifacts(self, *, now: datetime | None = None) -> tuple[int, int]:
        retention = self.maintenance.retention
        if retention is None:
            return (0, 0)
        current = now or utc_now()
        if self.next_retention_attempt_at is not None and current < self.next_retention_attempt_at:
            return (0, 0)
        if (
            self.last_retention_at is not None
            and (current - self.last_retention_at).total_seconds() < self.retention_interval_seconds
        ):
            return (0, 0)
        try:
            result = retention.reconcile(now=current)
        except Exception:
            LOGGER.exception("scheduler artifact retention failed")
            self.retention_consecutive_failures += 1
            retry_seconds = min(
                self.retention_retry_max_seconds,
                self.retention_retry_initial_seconds
                * (1 << (self.retention_consecutive_failures - 1)),
            )
            self.next_retention_attempt_at = current + timedelta(seconds=retry_seconds)
            return (0, 1)
        self.last_retention_at = current
        self.next_retention_attempt_at = None
        self.retention_consecutive_failures = 0
        return (result.removed, 0)

    def prune_events(self) -> int:
        """Delete events past the platform retention windows: audit events on the
        long window, control-loop/request telemetry on the short one, and worker
        events on the audit window."""
        pruned = int(self.runtime_services.events.prune())
        return pruned + WorkerEventService(self.runtime_services.context).prune()

    def _best_effort_prune_events(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        if (
            self.last_event_prune_at is not None
            and (current - self.last_event_prune_at).total_seconds()
            < self.event_prune_interval_seconds
        ):
            return 0
        try:
            pruned = self.prune_events()
        except Exception:
            LOGGER.exception("scheduler event pruning failed")
            return 0
        self.last_event_prune_at = current
        return pruned

    def prune_expired_tokens(self, *, now: datetime | None = None) -> int:
        """Delete expired platform-minted auth artifacts: system tokens
        (container gateway auth, workers, machines) and device-login codes."""
        context = self.runtime_services.context
        pruned = AuthService(context).prune_expired_system_tokens(now=now)
        return pruned + DeviceAuthorizationService(context).prune_expired(now=now)

    def _best_effort_prune_expired_tokens(self, *, now: datetime | None = None) -> int:
        current = now or utc_now()
        if (
            self.last_token_prune_at is not None
            and (current - self.last_token_prune_at).total_seconds()
            < self.token_prune_interval_seconds
        ):
            return 0
        try:
            pruned = self.prune_expired_tokens(now=current)
        except Exception:
            LOGGER.exception("scheduler expired token pruning failed")
            return 0
        self.last_token_prune_at = current
        return pruned

    def cleanup_workers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[WorkerRemovalResult]:
        workers = self.container_scheduler.workers
        if not isinstance(workers, WorkerCleanupRepository):
            return []
        return workers.cleanup_missing_workers(now=now)

    def _best_effort_cleanup_workers(
        self,
        *,
        now: datetime | None,
    ) -> list[WorkerRemovalResult]:
        try:
            return self.cleanup_workers(now=now)
        except Exception:
            LOGGER.exception("scheduler worker cleanup failed")
            return []

    def reconcile_orphaned_containers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        request_service = self.workloads.containers
        if self.services is None or request_service is None:
            return []
        current_time = now or utc_now()
        if (
            self.last_orphaned_container_reconcile_at is not None
            and (current_time - self.last_orphaned_container_reconcile_at).total_seconds()
            < self.orphaned_container_reconcile_interval_seconds
        ):
            return []
        self.last_orphaned_container_reconcile_at = current_time

        failed: list[str] = []
        expired = request_service.assignments.expired_assignments(
            before=current_time - timedelta(seconds=CONTAINER_DELIVERY_DEADLINE_SECONDS),
            limit=500,
        )
        for container, assigned_at in expired:
            recoverable = request_service.workers.has_recoverable_container_request(
                container.id, worker_id=container.runtime_worker_id
            )
            if (
                not recoverable
                and (current_time - assigned_at).total_seconds() < CONTAINER_START_DEADLINE_SECONDS
            ):
                continue
            stopped = self.runtime_services.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
                only_if_pending=True,
            )
            if stopped.status is ContainerStatus.Stopped:
                failed.append(container.id)

        confirmations = self.states.orphaned_container_confirmations
        if confirmations is None:
            return failed
        active = self.runtime_services.containers.list(
            statuses=(ContainerStatus.Pending, ContainerStatus.Running),
        )

        for container in active:
            state = request_service.containers.get_container_state(container.id)
            recoverable_request = request_service.workers.has_recoverable_container_request(
                container.id,
                worker_id=container.runtime_worker_id,
            )
            if state is not None or recoverable_request:
                confirmations.forget(container.id)
                continue
            if container.runtime_worker_id:
                worker = request_service.workers.get_worker(container.runtime_worker_id)
                if (
                    worker is not None
                    and worker.request_intake_status(at=current_time)
                    is SchedulerWorkerStatus.Available
                ):
                    confirmations.forget(container.id)
                    continue
            observed_at = confirmations.first_observed_at(
                container.id,
                now=current_time,
                ttl_seconds=int(self.orphaned_container_confirmation_seconds * 10),
            )
            if (
                current_time - observed_at
            ).total_seconds() < self.orphaned_container_confirmation_seconds:
                continue
            # Removing the record is the claim. Another scheduler that reached the
            # same conclusion finds it gone and leaves the container alone, so it
            # is failed once rather than once per scheduler.
            if not confirmations.claim_confirmed(container.id):
                continue
            if container.runtime_worker_id or container.status is ContainerStatus.Running:
                self.runtime_services.containers.stop(
                    container.id, reason=StopContainerReason.Scheduler
                )
            else:
                if not request_service.failure_handler.mark_scheduling_failed(
                    container.id,
                    workspace_id=container.workspace_id,
                    reason=ORPHANED_CONTAINER_FAILURE_REASON,
                    now=current_time,
                ):
                    continue
                request_service.containers.delete_container_state(container.id)
                orphaned_container_networks = self.states.orphaned_container_networks
                if orphaned_container_networks is not None:
                    orphaned_container_networks.remove_container_ips(container.id)
            failed.append(container.id)
        return failed

    def _best_effort_reconcile_orphaned_containers(
        self,
        *,
        now: datetime | None,
    ) -> list[str]:
        try:
            return self.reconcile_orphaned_containers(now=now)
        except Exception:
            LOGGER.exception("scheduler orphaned-container reconciliation failed")
            return []

    def recover_unsettled_preemptions(self, *, limit: int = 100) -> list[str]:
        """Settle preemption intents a control-plane crash left stranded.

        The API records the intent in the same transaction as the container's terminal
        state, so anything still unsettled is work whose inline settle never ran.
        """
        recovery = self.workloads.preemption_recovery
        if recovery is None:
            return []
        return recovery.recover_unsettled(limit=limit)

    def _best_effort_recover_unsettled_preemptions(self, *, limit: int) -> list[str]:
        try:
            return self.recover_unsettled_preemptions(limit=limit)
        except Exception:
            LOGGER.exception("scheduler preemption recovery failed")
            return []

    def _best_effort_reconcile_agent_pools(
        self,
        *,
        now: datetime | None,
    ) -> list[AgentPoolReconcileResult]:
        try:
            return self.reconcile_agent_pools(now=now)
        except Exception:
            LOGGER.exception("scheduler agent pool reconciliation failed")
            return []

    def _best_effort_expire_containers(
        self,
        *,
        now: datetime | None,
    ) -> list[ContainerRecord]:
        cron_job_locks = self.states.cron_job_locks
        if cron_job_locks is None:
            return []
        lock_key = cron_job_locks.key("scheduler", "leases", "container-expiry")
        token = uuid4().hex
        if not try_acquire_token_lock(
            cron_job_locks,
            lock_key,
            token,
            ttl_seconds=CONTAINER_EXPIRY_LOCK_TTL_SECONDS,
        ):
            return []
        try:
            return self.runtime_services.containers.expire_containers(now=now)
        except Exception:
            LOGGER.exception("scheduler container expiry failed")
            return []
        finally:
            release_token_lock(cron_job_locks, lock_key, token)

    def _best_effort_refresh_pool_states(
        self,
        *,
        now: datetime | None,
    ) -> dict[str, WorkerPoolStateSnapshot]:
        try:
            return self.refresh_pool_states(now=now)
        except Exception:
            LOGGER.exception("scheduler pool state refresh failed")
            return {}

    def _best_effort_reconcile_capacity_reservations(
        self,
        *,
        now: datetime | None,
    ) -> list[CapacityProvisioningReservation]:
        service = self.capacity.capacity_reservations
        containers = self.workloads.containers
        if service is None or containers is None:
            return []
        try:
            return service.reconcile(containers.workers.list_workers(), now=now)
        except Exception:
            LOGGER.exception("scheduler capacity reservation reconciliation failed")
            return []

    def _best_effort_reconcile_capacity_interruptions(
        self,
        *,
        now: datetime | None,
    ) -> list[WorkerPreemptionResult]:
        service = self.capacity.capacity_interruptions
        if service is None:
            return []
        try:
            return service.reconcile(now=now)
        except Exception:
            LOGGER.exception("scheduler capacity interruption reconciliation failed")
            return []

    def _best_effort_reconcile_managed_compute(
        self,
        *,
        now: datetime | None,
    ) -> list[PrivateUnitState]:
        # Both skips below are silent by design and between them they decide
        # whether a configured floor ever becomes a machine. Saying which one
        # declined is the difference between reading a log and reading the
        # source: the symptom of either is a pool that exists and an account
        # with no capacity, and neither raises anything.
        if self.services is None:
            LOGGER.info("managed compute reconciliation skipped: no scheduler services")
            return []
        current_time = now or utc_now()
        if (
            self.last_managed_compute_reconcile_at is not None
            and (current_time - self.last_managed_compute_reconcile_at).total_seconds()
            < self.managed_compute_reconcile_interval_seconds
        ):
            return []
        self.last_managed_compute_reconcile_at = current_time
        try:
            for unit in self.runtime_services.compute.empty_joined_units():
                try:
                    deleted = self.runtime_services.compute.delete_empty_joined_unit(unit)
                except CapacityReservationLockContendedError:
                    continue
                if deleted:
                    self.compute_states.delete_unit_state(unit.workspace_id, unit.capacity_owner_id)
                    self.pool_states.pool_states.delete_unit_state(unit.capacity_owner_id)
            self.runtime_services.compute.reconcile_pooled_capacity(now=current_time)
            return []
        except Exception:
            LOGGER.exception("scheduler managed compute reconciliation failed")
            return []

    def _best_effort_plan_reserves(self, *, now: datetime | None) -> None:
        """Plan platform headroom on its minute, sooner when a market stays short.

        Every pass reads the workers' free capacity from Redis; compute plans only
        when it holds the fleet-wide claim, so a replica's pass costs no database
        read until its turn.
        """
        if self.services is None:
            return
        current_time = now or utc_now()
        compute = self.runtime_services.compute
        try:
            workers = (
                self.workloads.containers.workers.list_workers()
                if self.workloads.containers is not None
                else []
            )
            early = compute.observe_reserve_pressure(
                reserve_free_capacity(workers, now=current_time), now=current_time
            )
            plan = compute.reconcile_platform_reserves(
                now=current_time,
                early=early,
                workers=workers,
                release=DeploymentReleaseService().active(),
            )
            if self.capacity.consolidation is not None:
                self.capacity.consolidation.reconcile(plan, now=current_time)
        except Exception:
            LOGGER.exception("platform reserve planning failed")

    def _best_effort_reconcile_release(self, *, now: datetime | None) -> None:
        """Reconcile on worker state changes and the periodic recovery sweep."""
        if self.services is None:
            return
        try:
            releases = DeploymentReleaseService()
            release = releases.active()
            controlled = release is not None and releases.controls(release)
        except Exception:
            LOGGER.exception("reading the active release failed; runtime updates wait")
            return
        if release is None or not controlled:
            return
        current_time = now or utc_now()
        if self.workloads.containers is None:
            LOGGER.error("release reconciliation requires worker inventory")
            return
        try:
            workers = self.workloads.containers.workers.list_workers()
        except Exception:
            LOGGER.exception("reading release worker inventory failed")
            return
        fingerprint = hashlib.sha256(
            repr(
                sorted(
                    (
                        worker.worker_id,
                        worker.runtime_image,
                        worker.agent_binary_sha256,
                        worker.status.value,
                        worker.request_intake_status(at=current_time).value,
                    )
                    for worker in workers
                )
            ).encode()
        ).hexdigest()
        changed = fingerprint != self.last_release_worker_fingerprint
        self.last_release_worker_fingerprint = fingerprint
        activated = (
            self.last_release_generation != 0 and release.generation != self.last_release_generation
        )
        self.last_release_generation = release.generation
        due = (
            self.last_release_reconcile_at is None
            or (current_time - self.last_release_reconcile_at).total_seconds()
            >= self.managed_compute_reconcile_interval_seconds
        )
        if not activated and not due and not changed:
            return
        if activated:
            LOGGER.info("release generation %d is active; reconciling runtimes", release.generation)
        self.last_release_reconcile_at = current_time
        try:
            redis = self.states.cron_job_locks
            if redis is None:
                raise RuntimeError("release reconciliation requires shared coordination")
            if not try_acquire_token_lock(
                redis,
                redis.key(
                    "scheduler",
                    "leases",
                    "release-reconciliation",
                    str(release.generation),
                    fingerprint if changed else "periodic",
                ),
                uuid4().hex,
                ttl_seconds=max(int(self.managed_compute_reconcile_interval_seconds), 1),
            ):
                return
            ComputeReleaseRolloutService(self.runtime_services.compute).reconcile(
                release,
                workers,
                now=current_time,
            )
            self.runtime_services.compute.refresh_stale_reserves(release, now=current_time)
        except CapacityReservationLockContendedError:
            LOGGER.debug("release reconciliation deferred: another controller holds the lease")
        except Exception:
            LOGGER.exception("scheduler release reconciliation failed")

    def _best_effort_reconcile_custom_domains(self, *, now: datetime | None) -> int:
        """Advance domains still waiting on the edge.

        Best effort and interval-gated like its neighbours: a certificate arriving
        late is visible state, never a reason to fail a scheduler tick.
        """
        custom_domains = self.maintenance.custom_domains
        if custom_domains is None:
            return 0
        current_time = now or utc_now()
        if (
            self.last_custom_domain_reconcile_at is not None
            and (current_time - self.last_custom_domain_reconcile_at).total_seconds()
            < self.custom_domain_reconcile_interval_seconds
        ):
            return 0
        self.last_custom_domain_reconcile_at = current_time
        try:
            return custom_domains.reconcile_due(now=current_time)
        except Exception:
            LOGGER.exception("scheduler custom domain reconciliation failed")
            return 0

    def _best_effort_drain_worker_pools(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> list[WorkerPoolDrainResult]:
        current_time = now or utc_now()
        # Provider inventory is needed for retirement, not for each dispatch tick.
        if (
            self.last_worker_pool_drain_at is not None
            and (current_time - self.last_worker_pool_drain_at).total_seconds()
            < self.worker_pool_drain_interval_seconds
        ):
            return []
        self.last_worker_pool_drain_at = current_time
        try:
            return self.drain_worker_pools(now=current_time, limit=limit)
        except Exception:
            LOGGER.exception("scheduler worker-pool drain failed")
            return []

    def _agent_pool_configs(self) -> list[AgentPoolConfig]:
        configs = self.capacity.agent_pool_configs
        if configs is None:
            msg = "scheduler agent pool configuration provider was not injected"
            raise RuntimeError(msg)
        return configs()

    def run_once(
        self, *, now: datetime | None = None, container_limit: int = 100
    ) -> SchedulerRunResult:
        return _merge_run_results(
            self.run_acquisition_pass(now=now, container_limit=container_limit),
            self.run_capacity_pass(now=now, container_limit=container_limit),
            self.run_housekeeping_pass(now=now, container_limit=container_limit),
        )
