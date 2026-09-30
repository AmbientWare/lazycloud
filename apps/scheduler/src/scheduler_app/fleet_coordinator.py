from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from billing.enforcement import BillingEnforcementResult
from compute.capacity_errors import CapacityReservationLockContendedError
from compute.state import RedisComputeStateRepository
from control.releases import DeploymentReleaseService
from coordination.token_lock import try_acquire_token_lock
from coordination.wake_signal import WakeSignalWaiter
from execution.containers.preemption import PreemptedContainerService
from scheduler.agent_pool import AgentPoolConfig, SchedulerAgentPoolService
from scheduler.capacity_reservations import CapacityReservationService
from scheduler.containers import SchedulerContainerRequestService
from scheduler.orphan_recovery import OrphanedContainerRecovery
from scheduler.pool_drain import WorkerPoolDrainResult, WorkerPoolDrainService
from scheduler.pool_state import SchedulerPoolStateService
from scheduler.preemption import SchedulerCapacityInterruptionService
from scheduler.reconciliation import (
    SchedulerRunResult,
    _log_worker_pool_drain_decisions,
    _merge_run_results,
    _record_worker_pool_drain_observability,
)
from scheduler.reserves import FleetConsolidationService, reserve_free_capacity
from scheduler.state import RedisSchedulerWorkerRepository
from shared.scheduling import SchedulerWorkerRecord
from shared.timestamps import utc_now

from scheduler_app.fleet_housekeeping import FleetHousekeeping
from scheduler_app.fleet_pass import FleetPass
from scheduler_app.fleet_services import FleetAppServices

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FleetCoordinator:
    services: FleetAppServices
    requests: SchedulerContainerRequestService
    workers: RedisSchedulerWorkerRepository
    wake: WakeSignalWaiter
    preemptions: PreemptedContainerService
    orphans: OrphanedContainerRecovery
    compute_states: RedisComputeStateRepository
    pool_states: SchedulerPoolStateService
    agent_pools: SchedulerAgentPoolService
    agent_configs: Callable[[], list[AgentPoolConfig]]
    reservations: CapacityReservationService
    drains: WorkerPoolDrainService
    interruptions: SchedulerCapacityInterruptionService
    consolidation: FleetConsolidationService | None
    housekeeping: FleetHousekeeping
    managed_compute_reconcile_interval_seconds: float = 60
    last_release_reconcile_at: datetime | None = field(default=None, init=False)
    last_release_generation: int = field(default=0, init=False)
    last_release_worker_fingerprint: str = field(default="", init=False)
    worker_pool_drain_logged_at: dict[str, datetime] = field(default_factory=dict)
    worker_pool_drain_event_signatures: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def run_acquisition_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
        retry_container_ids: Sequence[str] = (),
    ) -> SchedulerRunResult:
        if not include_containers:
            return SchedulerRunResult()
        run = FleetPass(self.services.redis_client)
        if not retry_container_ids:
            run.run(
                "capacity_recovery",
                lambda: self.services.compute.recovery.reconcile(
                    now=now or utc_now(), limit=container_limit
                ),
                None,
                interval=5,
            )
        sweep = self.requests.acquire_capacity(
            now=now, limit=container_limit, container_ids=retry_container_ids or None
        )
        run.finish()
        return SchedulerRunResult(capacity_demand_contended=list(sweep.contended))

    def run_capacity_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
    ) -> SchedulerRunResult:
        current = now or utc_now()
        run = FleetPass(self.services.redis_client)
        billing = run.run(
            "billing_enforcement",
            lambda: self.services.billing_enforcement.enforce(now=current),
            BillingEnforcementResult(),
            interval=5,
        )
        result = SchedulerRunResult(
            billing_enforcement_unfunded_count=billing.unfunded_count,
            billing_enforcement_stopped_count=billing.stopped_count,
            billing_enforcement_failure_count=billing.failed_count,
        )
        if include_containers:
            run.run(
                "capacity",
                lambda: self.reconcile_capacity(run, result, current, container_limit),
                None,
                interval=5,
            )
        run.finish()
        return result

    def reconcile_capacity(
        self, run: FleetPass, result: SchedulerRunResult, now: datetime, limit: int
    ) -> None:
        result.app_lifecycle_reconciliations = run.run(
            "apps", lambda: self.services.apps.reconcile_pending(limit=limit), []
        )
        result.capacity_interruptions = run.run(
            "interruptions", lambda: self.interruptions.reconcile(now=now), []
        )
        result.expired_containers = run.run(
            "container_expiry", lambda: self.services.containers.expire_containers(now=now), []
        )
        result.worker_cleanups = run.run(
            "worker_cleanup", lambda: self.workers.cleanup_missing_workers(now=now), []
        )
        result.settled_preemptions = run.run(
            "preemptions", lambda: self.preemptions.recover_unsettled(limit=limit), []
        )
        configs = self.agent_configs()
        result.agent_pool_reconciliations = run.run(
            "agent_pools", lambda: self.agent_pools.reconcile(configs, now=now), []
        )
        workers = self.workers.list_workers()
        run.run("reserves", lambda: self.plan_reserves(workers, now=now), None)
        run.run(
            "managed_compute",
            lambda: self.reconcile_managed_compute(now=now),
            None,
            interval=self.managed_compute_reconcile_interval_seconds,
        )
        workers = self.workers.list_workers()
        run.run("release", lambda: self.reconcile_release(workers, now=now), None)
        # Rollout can revoke intake. Refresh before projecting availability or
        # fulfilling a reservation from the worker's capacity.
        workers = self.workers.list_workers()
        result.pool_states = run.run(
            "pool_states",
            lambda: self.pool_states.refresh(agent_pool_configs=configs, workers=workers, now=now),
            {},
        )
        result.capacity_reservations = run.run(
            "reservations", lambda: self.reservations.reconcile(workers, now=now), []
        )
        result.orphaned_containers_failed = run.run(
            "orphans", lambda: self.orphans.reconcile_orphaned_containers(now=now), [], interval=30
        )
        result.worker_pool_drains = run.run(
            "worker_pool_drains",
            lambda: self.drain_worker_pools(now=now, limit=limit),
            [],
            interval=30,
        )

    def reconcile_managed_compute(self, *, now: datetime) -> None:
        compute = self.services.compute
        for unit in compute.units.empty_joined_units():
            try:
                deleted = compute.units.delete_empty_joined_unit(unit)
            except CapacityReservationLockContendedError:
                continue
            if deleted:
                self.compute_states.delete_unit_state(unit.workspace_id, unit.capacity_owner_id)
                self.pool_states.pool_states.delete_unit_state(unit.capacity_owner_id)
        compute.reconciliation.reconcile_pooled_capacity(now=now)

    def plan_reserves(self, workers: list[SchedulerWorkerRecord], *, now: datetime) -> None:
        compute = self.services.compute
        early = compute.reserves.observe_reserve_pressure(
            reserve_free_capacity(workers, now=now), now=now
        )
        plan = compute.reserves.reconcile_platform_reserves(
            now=now, early=early, workers=workers, release=DeploymentReleaseService().active()
        )
        if self.consolidation is not None:
            self.consolidation.reconcile(plan, now=now)

    def drain_worker_pools(self, *, now: datetime, limit: int) -> list[WorkerPoolDrainResult]:
        results = self.drains.reconcile(now=now, limit=limit)
        _record_worker_pool_drain_observability(
            self.services, results, event_signatures=self.worker_pool_drain_event_signatures
        )
        _log_worker_pool_drain_decisions(
            results, logged_at=self.worker_pool_drain_logged_at, now=now
        )
        return results

    def reconcile_release(self, workers: list[SchedulerWorkerRecord], *, now: datetime) -> None:
        releases = DeploymentReleaseService()
        release = releases.active()
        controlled = release is not None and releases.controls(release)
        if release is None or not controlled:
            return
        current_time = now
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
            redis = self.services.redis_client
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
            self.services.compute.rollouts.reconcile(
                release,
                workers,
                now=current_time,
            )
            self.services.compute.reserve_machines.refresh_stale_reserves(release, now=current_time)
        except CapacityReservationLockContendedError:
            LOGGER.debug("release reconciliation deferred: another controller holds the lease")

    def run_housekeeping_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
    ) -> SchedulerRunResult:
        return self.housekeeping.run(
            now=now, include_containers=include_containers, limit=container_limit
        )

    def run_once(
        self, *, now: datetime | None = None, container_limit: int = 100
    ) -> SchedulerRunResult:
        return _merge_run_results(
            self.run_acquisition_pass(now=now, container_limit=container_limit),
            self.run_capacity_pass(now=now, container_limit=container_limit),
            self.run_housekeeping_pass(now=now, container_limit=container_limit),
        )
