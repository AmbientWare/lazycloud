from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Protocol, runtime_checkable
from uuid import uuid4

from compute.projection import PrivateUnitState
from compute.state import RedisComputeStateRepository
from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient
from coordination.token_lock import release_token_lock, try_acquire_token_lock
from coordination.wake_signal import WakeSignalWaiter
from database.records.apps import AppRecord
from database.repositories.apps import CronJobRepository
from database.repositories.execution import CronJobRunRepository
from identity.auth import AuthService
from identity.device_auth import DeviceAuthorizationService
from observability.usage import WorkerEventService
from pydantic import Field, JsonValue
from shared.containers import ContainerRecord, ContainerStatus
from shared.contracts import ContractModel
from shared.cron import CronJobRecord, CronJobRun, next_cron_run
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind
from shared.events import EventLevel
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody, FunctionInvokeResponse
from shared.http.workspace_changes import WorkspaceChangeType
from shared.scheduling import SchedulerWorkerRequest, WorkerRemovalResult
from shared.tasks import Task
from shared.timestamps import utc_now
from shared.worker_events import (
    WORKER_POOL_DRAIN_DECISION_ACTION,
)

from scheduler.agent_pool import (
    AgentPoolConfig,
    AgentPoolReconcileResult,
    SchedulerAgentPoolService,
)
from scheduler.autoscaling import (
    EndpointAutoscaleResult,
    EndpointAutoscalingService,
    PodAutoscaleResult,
    PodAutoscalingService,
    PodControl,
    TaskQueueAutoscaleResult,
    TaskQueueAutoscalingService,
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
from scheduler.services import SchedulerServices

LOGGER = logging.getLogger(__name__)
WORKER_POOL_DRAIN_SOURCE = "worker_pool.drain"
CRON_JOB_LOCK_TTL_SECONDS = 10
# Short enough that a scheduler dying mid-sweep does not hold expiry shut for
# long, and long enough that one sweep finishes inside it.
POD_EXPIRY_LOCK_TTL_SECONDS = 30
CRON_JOB_DEPLOYMENT_KINDS = {DeploymentKind.Function, DeploymentKind.CronJob}
SCHEDULER_FAILURE_RETRY_MAX_SECONDS = 30.0
CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS = 1.0
MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS = 60.0
ORPHANED_CONTAINER_RECONCILE_INTERVAL_SECONDS = 30.0
ORPHANED_CONTAINER_CONFIRMATION_SECONDS = 60.0
ORPHANED_CONTAINER_FAILURE_REASON = (
    "container execution state was lost before the workload reached a recoverable runtime"
)


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


class SchedulerVolumeMeteringService(Protocol):
    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> SchedulerVolumeMeteringBatch: ...


class SchedulerRetentionBatch(Protocol):
    @property
    def removed(self) -> int: ...


class SchedulerRetentionService(Protocol):
    def reconcile(self, *, now: datetime | None = None) -> SchedulerRetentionBatch: ...


class SchedulerTailnetCleanupBatch(Protocol):
    @property
    def processed_count(self) -> int: ...

    @property
    def completed_count(self) -> int: ...

    @property
    def failure_count(self) -> int: ...


class SchedulerTailnetCleanupService(Protocol):
    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> SchedulerTailnetCleanupBatch: ...


class SchedulerCustomDomainService(Protocol):
    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> int: ...


class SchedulerTailnetCleanupBacklog(Protocol):
    def pending_count(self) -> int: ...


class ScheduledFunctionControl(Protocol):
    def schedule_due_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Task]: ...

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse: ...


class SchedulerPreemptionRecovery(Protocol):
    def recover_unsettled(self, *, limit: int = 100) -> list[str]: ...


class UnavailableTailnetCleanupBatch(ContractModel):
    processed_count: int = 0
    completed_count: int = 0
    failure_count: int = 0


@dataclass(frozen=True, slots=True)
class UnavailableTailnetCleanupService:
    backlog: SchedulerTailnetCleanupBacklog

    def reconcile_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> UnavailableTailnetCleanupBatch:
        del now, limit
        return UnavailableTailnetCleanupBatch(
            failure_count=self.backlog.pending_count(),
        )


def next_run_after(expression: str, now: datetime | None = None) -> datetime:
    return next_cron_run(expression, now)


def is_due(cron_job: CronJobRecord, now: datetime | None = None) -> bool:
    current = now or utc_now()
    if not cron_job.enabled:
        return False
    if cron_job.next_run_at is None:
        return True
    return cron_job.next_run_at <= current


class CronJobRunDraft(ContractModel):
    workspace_id: str
    cron_job: str
    enqueued: bool
    message_id: str | None = None
    task_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerWorkloadControls:
    containers: SchedulerContainerRequestService | None = None
    dispatch_wake: WakeSignalWaiter | None = None
    task_queues: TaskQueueAutoscalingService | None = None
    endpoints: EndpointAutoscalingService | None = None
    pods: PodAutoscalingService | None = None
    pod_control: PodControl | None = None
    functions: ScheduledFunctionControl | None = None
    preemption_recovery: SchedulerPreemptionRecovery | None = None


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


@dataclass(frozen=True, slots=True)
class SchedulerMaintenanceControls:
    volume_metering: SchedulerVolumeMeteringService | None = None
    retention: SchedulerRetentionService | None = None
    tailnet_cleanup: SchedulerTailnetCleanupService | None = None
    custom_domains: SchedulerCustomDomainService | None = None


@dataclass
class Scheduler:
    services: SchedulerServices | None = None
    interval_seconds: float = 1.0
    workloads: SchedulerWorkloadControls = field(default_factory=SchedulerWorkloadControls)
    states: SchedulerStateStores = field(default_factory=SchedulerStateStores)
    capacity: SchedulerCapacityControls = field(default_factory=SchedulerCapacityControls)
    maintenance: SchedulerMaintenanceControls = field(default_factory=SchedulerMaintenanceControls)
    reconcile_agent_pools_enabled: bool = True
    managed_compute_reconcile_interval_seconds: float = MANAGED_COMPUTE_RECONCILE_INTERVAL_SECONDS
    last_managed_compute_reconcile_at: datetime | None = field(default=None, init=False)
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
    event_prune_interval_seconds: float = 3600.0
    last_event_prune_at: datetime | None = field(default=None, init=False)
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
    def task_queue_autoscaling_service(self) -> TaskQueueAutoscalingService:
        task_queues = self.workloads.task_queues
        if task_queues is None:
            msg = "scheduler task queue autoscaler was not injected"
            raise RuntimeError(msg)
        return task_queues

    @property
    def endpoint_autoscaling_service(self) -> EndpointAutoscalingService:
        endpoints = self.workloads.endpoints
        if endpoints is None:
            msg = "scheduler endpoint autoscaler was not injected"
            raise RuntimeError(msg)
        return endpoints

    @property
    def pod_autoscaling_service(self) -> PodAutoscalingService:
        pods = self.workloads.pods
        if pods is None:
            msg = "scheduler pod autoscaler was not injected"
            raise RuntimeError(msg)
        return pods

    @property
    def cron_job_locks(self) -> RedisClient:
        cron_job_locks = self.states.cron_job_locks
        if cron_job_locks is None:
            msg = "scheduler cron job lock client was not injected"
            raise RuntimeError(msg)
        return cron_job_locks

    @property
    def pool_states(self) -> SchedulerPoolStateService:
        pools = self.states.pools
        if pools is None:
            msg = "scheduler pool state service was not injected"
            raise RuntimeError(msg)
        return pools

    @property
    def capacity_reservation_service(self) -> CapacityReservationService:
        service = self.capacity.capacity_reservations
        if service is None:
            msg = "scheduler capacity reservation service was not injected"
            raise RuntimeError(msg)
        return service

    @property
    def worker_pool_drain_service(self) -> WorkerPoolDrainService:
        service = self.capacity.worker_pool_drain
        if service is None:
            msg = "scheduler worker pool drain service was not injected"
            raise RuntimeError(msg)
        return service

    def tick(self, now: datetime | None = None) -> list[CronJobRun]:
        current = (now or utc_now()).astimezone(UTC)
        runs: list[CronJobRun] = []
        for cron_job in self.runtime_services.cron_jobs.list_all():
            if not is_due(cron_job, current):
                continue
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

    def reconcile_agent_pools(
        self,
        *,
        now: datetime | None = None,
    ) -> list[AgentPoolReconcileResult]:
        if not self.reconcile_agent_pools_enabled:
            return []
        configs = self._agent_pool_configs()
        if not configs:
            return []
        return self.agent_pool_service.reconcile(configs, now=now)

    def reconcile_task_queues(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[TaskQueueAutoscaleResult]:
        return self.task_queue_autoscaling_service.reconcile(now=now, limit=limit)

    def reconcile_endpoints(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[EndpointAutoscaleResult]:
        return self.endpoint_autoscaling_service.reconcile(now=now, limit=limit)

    def reconcile_pods(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[PodAutoscaleResult]:
        return self.pod_autoscaling_service.reconcile(now=now, limit=limit)

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
        return results

    def run_once(
        self,
        *,
        now: datetime | None = None,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        include_container_dispatch: bool = True,
        container_limit: int = 100,
    ) -> SchedulerRunResult:
        app_lifecycle_reconciliations = (
            self._best_effort_reconcile_app_lifecycle(limit=container_limit)
            if include_containers
            else []
        )
        capacity_interruptions = (
            self._best_effort_reconcile_capacity_interruptions(now=now)
            if include_containers
            else []
        )
        volume_metering_count, volume_metering_failure_count = self._meter_persistent_volumes(
            now=now,
            limit=container_limit,
        )
        expired_pods = self._best_effort_expire_pods(now=now) if include_containers else []
        worker_cleanups = self._best_effort_cleanup_workers(now=now) if include_containers else []
        task_queue_autoscaling = (
            self._best_effort_reconcile_task_queues(now=now, limit=container_limit)
            if include_containers
            else []
        )
        endpoint_autoscaling = (
            self._best_effort_reconcile_endpoints(now=now, limit=container_limit)
            if include_containers
            else []
        )
        pod_autoscaling = (
            self._best_effort_reconcile_pods(now=now, limit=container_limit)
            if include_containers
            else []
        )
        settled_preemptions = (
            self._best_effort_recover_unsettled_preemptions(limit=container_limit)
            if include_containers
            else []
        )
        function_retries = (
            self.schedule_function_retries(now=now, limit=container_limit)
            if include_containers
            else []
        )
        agent_pool_reconciliations = (
            self._best_effort_reconcile_agent_pools(now=now) if include_containers else []
        )
        managed_compute_reconciliations = (
            self._best_effort_reconcile_managed_compute(now=now) if include_containers else []
        )
        pool_states = self._best_effort_refresh_pool_states(now=now) if include_containers else {}
        capacity_reservations = (
            self._best_effort_reconcile_capacity_reservations(now=now) if include_containers else []
        )
        container_dispatches = (
            self.dispatch_containers(now=now, limit=container_limit)
            if include_containers and include_container_dispatch
            else []
        )
        orphaned_containers_failed = (
            self._best_effort_reconcile_orphaned_containers(now=now) if include_containers else []
        )
        tailnet_cleanup = self._best_effort_reconcile_tailnet_cleanup(
            now=now,
            limit=container_limit,
        )
        self._best_effort_reconcile_custom_domains(now=now)
        worker_pool_drains = (
            self._best_effort_drain_worker_pools(now=now, limit=container_limit)
            if include_containers
            else []
        )
        expired_tokens_pruned = (
            self._best_effort_prune_expired_tokens(now=now) if include_containers else 0
        )
        events_pruned = self._best_effort_prune_events(now=now) if include_containers else 0
        objects_removed, retention_failure_count = (
            self._best_effort_retain_artifacts(now=now) if include_containers else (0, 0)
        )
        return SchedulerRunResult(
            app_lifecycle_reconciliations=app_lifecycle_reconciliations,
            cron_job_runs=self.tick(now=now) if include_cron_jobs else [],
            function_retries=function_retries,
            agent_pool_reconciliations=agent_pool_reconciliations,
            task_queue_autoscaling=task_queue_autoscaling,
            endpoint_autoscaling=endpoint_autoscaling,
            pod_autoscaling=pod_autoscaling,
            expired_pods=expired_pods,
            pool_states=pool_states,
            capacity_reservations=capacity_reservations,
            capacity_interruptions=capacity_interruptions,
            managed_compute_reconciliations=managed_compute_reconciliations,
            tailnet_cleanup_processed_count=tailnet_cleanup[0],
            tailnet_cleanup_completed_count=tailnet_cleanup[1],
            tailnet_cleanup_failure_count=tailnet_cleanup[2],
            worker_pool_drains=worker_pool_drains,
            container_dispatches=container_dispatches,
            orphaned_containers_failed=orphaned_containers_failed,
            settled_preemptions=settled_preemptions,
            worker_cleanups=worker_cleanups,
            expired_tokens_pruned=expired_tokens_pruned,
            events_pruned=events_pruned,
            volume_metering_count=volume_metering_count,
            volume_metering_failure_count=volume_metering_failure_count,
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

        confirmations = self.states.orphaned_container_confirmations
        if confirmations is None:
            return []
        active = self.runtime_services.containers.list(
            statuses=(ContainerStatus.Pending, ContainerStatus.Running),
        )

        failed: list[str] = []
        for container in active:
            state = request_service.containers.get_container_state(container.id)
            recoverable_request = request_service.workers.has_recoverable_container_request(
                container.id,
                worker_id=container.runtime_worker_id,
            )
            if state is not None or recoverable_request:
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
            request = SchedulerWorkerRequest(
                workspace_id=container.workspace_id,
                stub_id=container.stub_id or "container",
                container_id=container.id,
            )
            request_service.containers.delete_container_state(container.id)
            orphaned_container_networks = self.states.orphaned_container_networks
            if orphaned_container_networks is not None:
                orphaned_container_networks.remove_container_ips(container.id)
            request_service.failure_handler.mark_scheduling_failed(
                request,
                ORPHANED_CONTAINER_FAILURE_REASON,
                now=current_time,
            )
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

    def _best_effort_reconcile_task_queues(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> list[TaskQueueAutoscaleResult]:
        try:
            return self.reconcile_task_queues(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler task queue autoscaling failed")
            return []

    def _best_effort_reconcile_endpoints(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> list[EndpointAutoscaleResult]:
        try:
            return self.reconcile_endpoints(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler endpoint autoscaling failed")
            return []

    def _best_effort_reconcile_pods(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> list[PodAutoscaleResult]:
        try:
            return self.reconcile_pods(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler pod autoscaling failed")
            return []

    def _best_effort_expire_pods(
        self,
        *,
        now: datetime | None,
    ) -> list[ContainerRecord]:
        pod_control = self.workloads.pod_control
        if pod_control is None:
            return []
        cron_job_locks = self.states.cron_job_locks
        if cron_job_locks is None:
            return []
        # Expiring a pod stops its container, cancels its task and announces both.
        # Nothing downstream of that is idempotent, and the decision is taken from
        # a read rather than a locked row, so two schedulers sweeping together
        # each act on the same expired pod.
        lock_key = cron_job_locks.key("scheduler", "leases", "pod-expiry")
        token = uuid4().hex
        if not try_acquire_token_lock(
            cron_job_locks,
            lock_key,
            token,
            ttl_seconds=POD_EXPIRY_LOCK_TTL_SECONDS,
        ):
            return []
        try:
            return pod_control.expire_pods(now=now)
        except Exception:
            LOGGER.exception("scheduler pod expiry failed")
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
        if self.services is None:
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
            self.runtime_services.compute.reconcile_pooled_capacity(now=current_time)
            return []
        except Exception:
            LOGGER.exception("scheduler managed compute reconciliation failed")
            return []

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

    def _best_effort_reconcile_tailnet_cleanup(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> tuple[int, int, int]:
        tailnet_cleanup = self.maintenance.tailnet_cleanup
        if tailnet_cleanup is None:
            return (0, 0, 0)
        try:
            batch = tailnet_cleanup.reconcile_due(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler tailnet cleanup reconciliation failed")
            return (0, 0, 1)
        if batch.failure_count:
            LOGGER.warning(
                "scheduler tailnet cleanup remains incomplete",
                extra={
                    "tailnet_cleanup_processed_count": batch.processed_count,
                    "tailnet_cleanup_completed_count": batch.completed_count,
                    "tailnet_cleanup_failure_count": batch.failure_count,
                },
            )
        return (batch.processed_count, batch.completed_count, batch.failure_count)

    def _best_effort_drain_worker_pools(
        self,
        *,
        now: datetime | None,
        limit: int,
    ) -> list[WorkerPoolDrainResult]:
        try:
            return self.drain_worker_pools(now=now, limit=limit)
        except Exception:
            LOGGER.exception("scheduler worker-pool drain failed")
            return []

    def run_forever(
        self,
        *,
        include_cron_jobs: bool = True,
        include_containers: bool = True,
        container_limit: int = 100,
        beat: Callable[[], None] | None = None,
    ) -> None:
        dispatch_wake = self.workloads.dispatch_wake
        if include_containers and dispatch_wake is None:
            raise RuntimeError("scheduler dispatch wake waiter was not injected")
        stop_dispatch = Event()
        dispatch_thread: Thread | None = None
        if dispatch_wake is not None and include_containers:
            dispatch_thread = Thread(
                target=self.run_container_dispatch_loop,
                kwargs={
                    "stop": stop_dispatch,
                    "container_limit": container_limit,
                },
                name="scheduler-container-dispatch",
                daemon=True,
            )
            dispatch_thread.start()
        consecutive_failures = 0
        try:
            while True:
                if beat is not None:
                    beat()
                try:
                    self.run_once(
                        include_cron_jobs=include_cron_jobs,
                        include_containers=include_containers,
                        include_container_dispatch=False,
                        container_limit=container_limit,
                    )
                except Exception:
                    consecutive_failures += 1
                    retry_seconds = min(
                        SCHEDULER_FAILURE_RETRY_MAX_SECONDS,
                        max(self.interval_seconds, 0.1) * (1 << min(consecutive_failures - 1, 8)),
                    )
                    LOGGER.exception(
                        "scheduler pass failed; retrying",
                        extra={
                            "consecutive_failures": consecutive_failures,
                            "retry_seconds": retry_seconds,
                        },
                    )
                else:
                    consecutive_failures = 0
                    retry_seconds = max(self.interval_seconds, 0.0)
                time.sleep(retry_seconds)
        finally:
            stop_dispatch.set()
            if dispatch_thread is not None:
                dispatch_thread.join(timeout=CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS + 0.1)
                if dispatch_thread.is_alive():
                    LOGGER.warning("scheduler container dispatch loop did not stop promptly")

    def run_container_dispatch_loop(
        self,
        *,
        stop: Event,
        container_limit: int = 100,
    ) -> None:
        dispatch_wake = self.workloads.dispatch_wake
        if dispatch_wake is None:
            raise RuntimeError("scheduler dispatch wake waiter was not injected")
        while not stop.is_set():
            try:
                dispatch_wake.wait(
                    timeout_seconds=CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS,
                )
            except REDIS_UNAVAILABLE_ERRORS:
                LOGGER.exception("scheduler dispatch wake wait failed; periodic sweep delayed")
                stop.wait(CONTAINER_DISPATCH_SWEEP_INTERVAL_SECONDS)
                continue
            if stop.is_set():
                return
            try:
                self.drain_container_dispatches(limit=container_limit)
            except Exception:
                LOGGER.exception("scheduler container dispatch failed; periodic sweep will retry")

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
                stub_id = _cron_function_stub_id(cron_job, deployment)
                if not stub_id:
                    raise ValueError("cron job must reference its function-like deployment stub")
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
            CronJobRepository(session).upsert(cron_job, workspace_id=cron_job.workspace_id)
            saved_run = CronJobRunRepository(session).records.create(
                run.model_dump(mode="json"),
                workspace_id=cron_job.workspace_id,
            )
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
            response = functions.function_invoke(
                FunctionInvokeBody(
                    stub_id=stub_id,
                    invocation=FunctionJsonInvocation(),
                    headless=True,
                )
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

    def list_cron_job_runs(self, *, limit: int | None = None) -> list[CronJobRun]:
        with self.runtime_services.context.database.session() as session:
            runs = CronJobRunRepository(session).records.list_across_workspaces()
        runs.sort(key=lambda item: item.created_at, reverse=True)
        return runs[:limit] if limit is not None else runs

    def _agent_pool_configs(self) -> list[AgentPoolConfig]:
        configs = self.capacity.agent_pool_configs
        if configs is None:
            msg = "scheduler agent pool configuration provider was not injected"
            raise RuntimeError(msg)
        return configs()


class SchedulerRunResult(ContractModel):
    app_lifecycle_reconciliations: list[AppRecord] = Field(default_factory=list)
    cron_job_runs: list[CronJobRun] = Field(default_factory=list)
    function_retries: list[Task] = Field(default_factory=list)
    agent_pool_reconciliations: list[AgentPoolReconcileResult] = Field(default_factory=list)
    task_queue_autoscaling: list[TaskQueueAutoscaleResult] = Field(default_factory=list)
    endpoint_autoscaling: list[EndpointAutoscaleResult] = Field(default_factory=list)
    pod_autoscaling: list[PodAutoscaleResult] = Field(default_factory=list)
    expired_pods: list[ContainerRecord] = Field(default_factory=list)
    pool_states: dict[str, WorkerPoolStateSnapshot] = Field(default_factory=dict)
    capacity_reservations: list[CapacityProvisioningReservation] = Field(default_factory=list)
    capacity_interruptions: list[WorkerPreemptionResult] = Field(default_factory=list)
    managed_compute_reconciliations: list[PrivateUnitState] = Field(default_factory=list)
    tailnet_cleanup_processed_count: int = 0
    tailnet_cleanup_completed_count: int = 0
    tailnet_cleanup_failure_count: int = 0
    worker_pool_drains: list[WorkerPoolDrainResult] = Field(default_factory=list)
    container_dispatches: list[SchedulerContainerDispatchResult] = Field(default_factory=list)
    orphaned_containers_failed: list[str] = Field(default_factory=list)
    settled_preemptions: list[str] = Field(default_factory=list)
    worker_cleanups: list[WorkerRemovalResult] = Field(default_factory=list)
    expired_tokens_pruned: int = 0
    events_pruned: int = 0
    volume_metering_count: int = 0
    volume_metering_failure_count: int = 0
    objects_removed: int = 0
    retention_failure_count: int = 0


def _cron_function_stub_id(cron_job: CronJobRecord, deployment: Deployment) -> str:
    if deployment.kind not in CRON_JOB_DEPLOYMENT_KINDS:
        return ""
    if not deployment.stub_id:
        return ""
    payload = cron_job.payload
    if not isinstance(payload, dict):
        return ""
    stub_id = payload.get("stub_id")
    if not isinstance(stub_id, str):
        return ""
    normalized = stub_id.strip()
    return normalized if normalized == deployment.stub_id else ""


def _record_worker_pool_drain_observability(
    services: SchedulerServices,
    results: list[WorkerPoolDrainResult],
    *,
    event_signatures: dict[str, tuple[str, ...]],
) -> None:
    for result in results:
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
            or event_signatures.get(result.pool) != signature
        ):
            data: dict[str, JsonValue] = {
                "source": WORKER_POOL_DRAIN_SOURCE,
                "pool": result.pool,
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
                resource_id=result.pool,
                message="worker-pool drain selected desired capacity",
                level=EventLevel.Warning if result.error else EventLevel.Info,
                data=data,
            )
        event_signatures[str(result.pool)] = signature
        labels = {"source": WORKER_POOL_DRAIN_SOURCE, "pool": str(result.pool)}
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
