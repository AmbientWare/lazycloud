from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from uuid import uuid4

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient
from coordination.token_lock import release_token_lock, try_acquire_token_lock
from database.records.apps import AutoscalingStubRecord
from database.repositories.apps import CronJobRepository
from database.repositories.execution import (
    CronJobRunRepository,
)
from shared.cron import CronJobRecord, CronJobRun
from shared.function_payloads import FunctionJsonInvocation, FunctionPayloadEncoding
from shared.http.functions import FunctionInvokeBody
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task
from shared.timestamps import utc_now

from scheduler.autoscaling import (
    AutoscaleResult,
    AutoscalingPlacementSnapshot,
    load_autoscaling_placement_snapshot,
)
from scheduler.containers import (
    SchedulerContainerDispatchResult,
    SchedulerContainerRequestService,
)
from scheduler.reconciliation import (
    AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS,
    AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS,
    CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS,
    CRON_JOB_LOCK_TTL_SECONDS,
    DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    LOGGER,
    CronJobRunDraft,
    SchedulerCronJobRunPage,
    SchedulerRunResult,
    SchedulerStateStores,
    SchedulerWorkloadControls,
    _decode_cron_job_run_cursor,
    _encode_cron_job_run_cursor,
    _function_cron_job_lock_key,
    _merge_run_results,
    _next_autoscaling_target_reconcile,
    next_run_after,
)
from scheduler.services import SchedulerServices


@dataclass
class Scheduler:
    services: SchedulerServices | None = None
    workloads: SchedulerWorkloadControls = field(default_factory=SchedulerWorkloadControls)
    states: SchedulerStateStores = field(default_factory=SchedulerStateStores)

    @property
    def runtime_services(self) -> SchedulerServices:
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
    def cron_job_locks(self) -> RedisClient:
        cron_job_locks = self.states.cron_job_locks
        if cron_job_locks is None:
            msg = "scheduler cron job lock client was not injected"
            raise RuntimeError(msg)
        return cron_job_locks

    def tick(self, now: datetime | None = None, *, limit: int = 100) -> list[CronJobRun]:
        current = (now or utc_now()).astimezone(UTC)
        runs: list[CronJobRun] = []
        with self.runtime_services.context.database.session() as session:
            due = CronJobRepository(session).due_across_workspaces(
                now=current,
                limit=max(limit, 0),
            )
        for cron_job in due:
            run = self._run_cron_job(cron_job, current)
            runs.append(run)
        return runs

    def schedule_function_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Task]:
        if self.services is None:
            return []
        functions = self.workloads.functions
        if functions is None:
            msg = "scheduler function control was not injected"
            raise RuntimeError(msg)
        return functions.schedule_due_retries(
            now=now,
            limit=limit,
        )

    def dispatch_containers(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[SchedulerContainerDispatchResult]:
        return self.container_scheduler.dispatch_ready(now=now, limit=limit)

    def drain_container_dispatches(
        self,
        *,
        limit: int = 100,
    ) -> list[SchedulerContainerDispatchResult]:
        batch_limit = max(limit, 1)
        dispatched: list[SchedulerContainerDispatchResult] = []
        while True:
            batch = self.dispatch_containers(limit=batch_limit)
            dispatched.extend(batch)
            if len(batch) < batch_limit:
                return dispatched

    def run_placement_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    ) -> SchedulerRunResult:
        """Reconcile due autoscaling targets without recurring recovery scans."""

        if not include_containers:
            return SchedulerRunResult()
        current_time = now or utc_now()
        function_driver = self.workloads.function_autoscaler
        endpoint_driver = self.workloads.endpoints
        pod_driver = self.workloads.pods
        drivers = tuple(
            driver
            for driver in (function_driver, endpoint_driver, pod_driver)
            if driver is not None
        )
        if not drivers:
            return SchedulerRunResult()
        targets = self.workloads.autoscaling_targets
        if targets is None:
            raise RuntimeError("scheduler autoscaling target service was not injected")
        claims = targets.claim_due(
            now=current_time,
            limit=autoscaling_limit,
            lease_seconds=AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS,
        )
        if not claims:
            return SchedulerRunResult()
        stubs: tuple[AutoscalingStubRecord, ...] = ()
        snapshot = AutoscalingPlacementSnapshot(
            stubs=(),
            active_by_stub={},
            containers_by_stub={},
            scheduler_statuses={},
        )
        snapshot_failed = False
        if claims:
            try:
                stubs = tuple(
                    self.runtime_services.scheduler_workloads.list_autoscaling_stubs(
                        [claim.stub_id for claim in claims]
                    )
                )
                snapshot = load_autoscaling_placement_snapshot(
                    self.runtime_services,
                    drivers[0].container_states,
                    stubs,
                    now=current_time,
                )
            except Exception:
                snapshot_failed = True
                stubs = ()
                LOGGER.exception("scheduler placement snapshot failed")

        results_by_driver: dict[int, list[AutoscaleResult]] = {}
        failed_stub_ids: set[str] = set()
        if snapshot_failed:
            failed_stub_ids.update(claim.stub_id for claim in claims)
        else:
            for driver in drivers:
                selected_ids = {stub.id for stub in stubs if driver.selects(stub)}
                if not selected_ids:
                    results_by_driver[id(driver)] = []
                    continue
                try:
                    results_by_driver[id(driver)] = driver.reconcile_snapshot(
                        snapshot,
                        now=current_time,
                        limit=autoscaling_limit,
                    )
                except Exception:
                    failed_stub_ids.update(selected_ids)
                    results_by_driver[id(driver)] = []
                    LOGGER.exception("%s autoscaling failed", driver.workload.identity.kind.value)

        results_by_stub_id = {
            result.stub_id: result for results in results_by_driver.values() for result in results
        }
        retry_at = current_time + timedelta(seconds=AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS)
        targets.complete_many(
            [
                (
                    claim,
                    (
                        retry_at
                        if claim.stub_id in failed_stub_ids
                        else _next_autoscaling_target_reconcile(
                            results_by_stub_id.get(claim.stub_id),
                            now=current_time,
                        )
                    ),
                )
                for claim in claims
            ],
            now=current_time,
        )
        return SchedulerRunResult(
            function_autoscaling=(
                results_by_driver.get(id(function_driver), []) if function_driver else []
            ),
            endpoint_autoscaling=(
                results_by_driver.get(id(endpoint_driver), []) if endpoint_driver else []
            ),
            pod_autoscaling=(results_by_driver.get(id(pod_driver), []) if pod_driver else []),
        )

    def run_recovery_pass(
        self,
        *,
        now: datetime | None = None,
        include_containers: bool = True,
        container_limit: int = 100,
        autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    ) -> SchedulerRunResult:
        if not include_containers:
            return SchedulerRunResult()
        if not self._claim_cadence("recovery", seconds=1):
            return SchedulerRunResult()
        current_time = now or utc_now()
        functions = self.workloads.functions
        if functions is not None:
            functions.expire_timed_out_tasks(now=current_time, limit=container_limit)
        self.container_scheduler.recover_scheduling_requests(
            now=current_time, limit=container_limit
        )
        retries = self._best_effort_schedule_function_retries(
            now=current_time, limit=container_limit
        )
        return _merge_run_results(
            self.run_placement_pass(now=current_time, autoscaling_limit=autoscaling_limit),
            SchedulerRunResult(function_retries=retries),
        )

    def run_build_pass(self, *, container_limit: int = 100) -> SchedulerRunResult:
        if self.workloads.image_builds is not None:
            if self._claim_cadence("build-recovery", seconds=5):
                self.workloads.image_builds.recover(limit=container_limit)
            if self._claim_cadence("build-dispatch", seconds=1):
                self.workloads.image_builds.drain(limit=container_limit)
            if self._claim_cadence("build-cleanup", seconds=30):
                self.workloads.image_builds.cleanup(limit=container_limit)
        return SchedulerRunResult()

    def run_scheduled_pass(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> SchedulerRunResult:
        if not self._claim_cadence("cron", seconds=1):
            return SchedulerRunResult()
        return SchedulerRunResult(cron_job_runs=self.tick(now=now, limit=limit))

    def _claim_cadence(self, name: str, *, seconds: int) -> bool:
        # Retain the key until expiry so replicas cannot repeat a finished sweep.
        redis = self.cron_job_locks
        return try_acquire_token_lock(
            redis,
            redis.key("scheduler", f"{name}-cadence"),
            uuid4().hex,
            ttl_seconds=seconds,
        )

    def _best_effort_schedule_function_retries(
        self, *, now: datetime | None, limit: int
    ) -> list[Task]:
        """Retry due function tasks, and never let one of them end the pass.

        Best effort like its neighbours: one tenant whose task cannot be
        scheduled would otherwise end the pass for every other tenant, on every
        tick, and take every step after it with it.
        """

        try:
            return self.schedule_function_retries(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler function retry scheduling failed")
            return []

    def run_container_dispatch_loop(
        self,
        *,
        stop: Event,
        container_limit: int = 100,
        beat: Callable[[], None] | None = None,
    ) -> None:
        """Place what is ready, woken by arrival rather than by a clock.

        The beat is this loop's own. It waits on Redis rather than on a timer,
        so a wedge here looks nothing like a wedge in the loops that sleep, and
        a liveness check reading one file for the whole process would call this
        alive on the strength of a loop that is not this one.
        """

        dispatch_wake = self.workloads.dispatch_wake
        if dispatch_wake is None:
            raise RuntimeError("scheduler dispatch wake waiter was not injected")
        while not stop.is_set():
            try:
                notified = dispatch_wake.wait(
                    timeout_seconds=CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS,
                )
            except REDIS_UNAVAILABLE_ERRORS:
                LOGGER.exception("scheduler dispatch wake wait failed; periodic sweep delayed")
                stop.wait(CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS)
                continue
            if stop.is_set():
                return
            try:
                started = monotonic()
                results = self.drain_container_dispatches(limit=container_limit)
                elapsed = monotonic() - started
                for result in results:
                    LOGGER.info(
                        "container dispatch %s: status=%s notified=%s batch_seconds=%.3f reason=%s",
                        result.container_id,
                        result.status.value,
                        notified,
                        elapsed,
                        result.reason,
                        extra={"container_id": result.container_id, "worker_id": result.worker_id},
                    )
            except Exception:
                LOGGER.exception("scheduler container dispatch failed; periodic sweep will retry")
                continue
            if beat is not None:
                beat()

    def _run_cron_job(self, cron_job: CronJobRecord, now: datetime) -> CronJobRun:
        try:
            deployment = self.runtime_services.deployments.get(cron_job.deployment_id)
            if not deployment.active:
                run = CronJobRunDraft(
                    workspace_id=cron_job.workspace_id,
                    cron_job=cron_job.name,
                    enqueued=False,
                    reason="deployment inactive",
                )
            else:
                stub_id = deployment.stub_id or ""
                if not stub_id:
                    raise ValueError("scheduled deployment published no stub to invoke")
                run = self._run_cron_function(cron_job, stub_id)
        except Exception as exc:
            run = CronJobRunDraft(
                workspace_id=cron_job.workspace_id,
                cron_job=cron_job.name,
                enqueued=False,
                reason=str(exc),
            )

        cron_job.last_run_at = now
        cron_job.next_run_at = next_run_after(cron_job.cron, now)
        cron_job.updated_at = utc_now()
        with self.runtime_services.context.database.session() as session:
            updated = CronJobRepository(session).record_run(
                cron_job, workspace_id=cron_job.workspace_id
            )
            saved_run = CronJobRunRepository(session).append(
                CronJobRun(
                    id=str(uuid4()),
                    workspace_id=run.workspace_id,
                    cron_job=run.cron_job,
                    enqueued=run.enqueued,
                    task_id=run.task_id,
                    reason=run.reason,
                )
            )
        if updated:
            self.runtime_services.cron_jobs.publish_change(
                cron_job,
                WorkspaceChangeType.Updated,
            )
        return saved_run

    def _run_cron_function(
        self,
        cron_job: CronJobRecord,
        stub_id: str,
    ) -> CronJobRunDraft:
        token = uuid4().hex
        lock_key = self.cron_job_locks.key(_function_cron_job_lock_key(stub_id))
        if not self._acquire_cron_job_lock(lock_key, token):
            return CronJobRunDraft(
                workspace_id=cron_job.workspace_id,
                cron_job=cron_job.name,
                enqueued=False,
                reason="cron job lock not acquired",
            )
        try:
            functions = self.workloads.functions
            if functions is None:
                msg = "scheduler function control was not injected"
                raise RuntimeError(msg)
            stub = self.runtime_services.scheduler_workloads.get_stub(
                stub_id, workspace=cron_job.workspace_id
            )
            response = functions.function_invoke(
                FunctionInvokeBody(
                    stub_id=stub_id,
                    invocation=FunctionJsonInvocation(
                        result_encoding=FunctionPayloadEncoding.Cloudpickle
                    ),
                    headless=True,
                ),
                stub=stub,
            )
            accepted = bool(response.task_id and response.exit_code == 0)
            return CronJobRunDraft(
                workspace_id=cron_job.workspace_id,
                cron_job=cron_job.name,
                enqueued=accepted,
                task_id=response.task_id or None,
                reason=(None if accepted else response.output or "cron function invocation failed"),
            )
        finally:
            self._release_cron_job_lock(lock_key, token)

    def _acquire_cron_job_lock(self, key: str, token: str) -> bool:
        return try_acquire_token_lock(
            self.cron_job_locks,
            key,
            token,
            ttl_seconds=CRON_JOB_LOCK_TTL_SECONDS,
        )

    def _release_cron_job_lock(self, key: str, token: str) -> None:
        release_token_lock(self.cron_job_locks, key, token)

    def list_cron_job_runs(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> SchedulerCronJobRunPage:
        with self.runtime_services.context.database.session() as session:
            page = CronJobRunRepository(session).page(
                workspace_id=workspace_id,
                cursor=_decode_cron_job_run_cursor(cursor),
                limit=min(max(limit, 1), 1_000),
            )
        return SchedulerCronJobRunPage(
            data=page.data,
            next=_encode_cron_job_run_cursor(page.next),
        )

    def run_once(
        self,
        *,
        now: datetime | None = None,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        include_container_dispatch: bool = True,
        container_limit: int = 100,
        autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    ) -> SchedulerRunResult:
        return _merge_run_results(
            self.run_scheduled_pass(now=now, limit=container_limit)
            if include_cron_jobs
            else SchedulerRunResult(),
            self.run_build_pass(container_limit=container_limit)
            if include_containers
            else SchedulerRunResult(),
            self.run_recovery_pass(
                now=now,
                include_containers=include_containers,
                container_limit=container_limit,
                autoscaling_limit=autoscaling_limit,
            ),
            SchedulerRunResult(
                container_dispatches=self.dispatch_containers(now=now, limit=container_limit)
                if include_containers and include_container_dispatch
                else []
            ),
        )
