from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from coordination.redis_client import RedisClient
from coordination.token_lock import try_acquire_token_lock
from database.repositories.orchestration import AutoscalingTargetRepository
from shared.autoscaler_state import AutoscalerTargetKind
from shared.tasks import Task
from shared.timestamps import utc_now

from scheduler.autoscaling import AutoscaleResult, AutoscalingDriver
from scheduler.containers import SchedulerContainerRequestService
from scheduler.cron import CronScheduler
from scheduler.reconciliation import (
    AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS,
    AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS,
    DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    LOGGER,
    SchedulerRunResult,
    _merge_run_results,
    _next_autoscaling_target_reconcile,
)
from scheduler.services import SchedulerServices


class FunctionRecovery(Protocol):
    def expire_timed_out_tasks(self, *, now: datetime, limit: int = 100) -> None: ...

    def schedule_due_retries(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> list[Task]: ...


class BuildSubmissions(Protocol):
    def drain(self, *, limit: int = 16) -> int: ...

    def recover(self, *, limit: int = 100) -> int: ...

    def cleanup(self, *, limit: int = 16) -> None: ...


@dataclass
class Scheduler:
    services: SchedulerServices
    redis: RedisClient
    containers: SchedulerContainerRequestService
    functions: FunctionRecovery
    autoscalers: tuple[AutoscalingDriver, ...]
    image_builds: BuildSubmissions
    cron: CronScheduler

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
        with self.services.context.database.session() as session:
            claims = AutoscalingTargetRepository(session).claim_due(
                now=current_time,
                limit=autoscaling_limit,
                lease_seconds=AUTOSCALING_TARGET_CLAIM_LEASE_SECONDS,
            )
        if not claims:
            return SchedulerRunResult()
        results_by_kind: dict[AutoscalerTargetKind, list[AutoscaleResult]] = {}
        failed_stub_ids: set[str] = set()
        try:
            stubs = self.services.control_plane_service.list_autoscaling_stubs(
                [claim.stub_id for claim in claims]
            )
        except Exception:
            failed_stub_ids.update(claim.stub_id for claim in claims)
            LOGGER.exception("scheduler workload selection failed")
        else:
            for driver in self.autoscalers:
                selected = [stub for stub in stubs if driver.selects(stub)]
                if not selected:
                    continue
                try:
                    results_by_kind[driver.workload.identity.kind] = driver.reconcile(
                        selected,
                        now=current_time,
                        limit=autoscaling_limit,
                    )
                except Exception:
                    failed_stub_ids.update(stub.id for stub in selected)
                    LOGGER.exception("%s autoscaling failed", driver.workload.identity.kind.value)

        results_by_stub_id = {
            result.stub_id: result for results in results_by_kind.values() for result in results
        }
        retry_at = current_time + timedelta(seconds=AUTOSCALING_TARGET_RECONCILE_INTERVAL_SECONDS)
        with self.services.context.database.session() as session:
            AutoscalingTargetRepository(session).complete_many(
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
            function_autoscaling=results_by_kind.get(AutoscalerTargetKind.Function, []),
            endpoint_autoscaling=results_by_kind.get(AutoscalerTargetKind.Endpoint, []),
            pod_autoscaling=results_by_kind.get(AutoscalerTargetKind.Pod, []),
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
        self.functions.expire_timed_out_tasks(now=current_time, limit=container_limit)
        self.containers.recover_scheduling_requests(now=current_time, limit=container_limit)
        retries = self._best_effort_schedule_function_retries(
            now=current_time, limit=container_limit
        )
        return _merge_run_results(
            self.run_placement_pass(now=current_time, autoscaling_limit=autoscaling_limit),
            SchedulerRunResult(function_retries=retries),
        )

    def run_build_pass(self, *, container_limit: int = 100) -> SchedulerRunResult:
        if self._claim_cadence("build-recovery", seconds=5):
            self.image_builds.recover(limit=container_limit)
        if self._claim_cadence("build-dispatch", seconds=1):
            self.image_builds.drain(limit=container_limit)
        if self._claim_cadence("build-cleanup", seconds=30):
            self.image_builds.cleanup(limit=container_limit)
        return SchedulerRunResult()

    def run_scheduled_pass(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> SchedulerRunResult:
        if not self._claim_cadence("cron", seconds=1):
            return SchedulerRunResult()
        return SchedulerRunResult(cron_job_runs=self.cron.tick(now=now, limit=limit))

    def _claim_cadence(self, name: str, *, seconds: int) -> bool:
        # Retain the key until expiry so replicas cannot repeat a finished sweep.
        redis = self.redis
        return try_acquire_token_lock(
            redis,
            redis.key("scheduler", f"{name}-cadence"),
            uuid4().hex,
            ttl_seconds=seconds,
        )

    def _best_effort_schedule_function_retries(
        self, *, now: datetime | None, limit: int
    ) -> list[Task]:
        try:
            return self.functions.schedule_due_retries(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler function retry scheduling failed")
            return []

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
                container_dispatches=self.containers.dispatch_ready(now=now, limit=container_limit)
                if include_containers and include_container_dispatch
                else []
            ),
        )
