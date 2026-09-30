from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from shared.container_requests import StopContainerReason
from shared.containers import ContainerStatus
from shared.scheduling import SchedulerWorkerStatus
from shared.timestamps import utc_now

from scheduler.autoscaling import (
    CONTAINER_DELIVERY_DEADLINE_SECONDS,
    CONTAINER_START_DEADLINE_SECONDS,
)
from scheduler.containers import SchedulerContainerRequestService
from scheduler.reconciliation import (
    ORPHANED_CONTAINER_CONFIRMATION_SECONDS,
    ORPHANED_CONTAINER_FAILURE_REASON,
    OrphanedContainerConfirmationRepository,
    OrphanedContainerNetworkRepository,
)
from scheduler.services import SchedulerContainerService


@dataclass(frozen=True, slots=True)
class OrphanedContainerRecovery:
    containers: SchedulerContainerService
    requests: SchedulerContainerRequestService
    confirmations: OrphanedContainerConfirmationRepository
    networks: OrphanedContainerNetworkRepository
    confirmation_seconds: float = ORPHANED_CONTAINER_CONFIRMATION_SECONDS

    def reconcile_orphaned_containers(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        request_service = self.requests
        current_time = now or utc_now()

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
            stopped = self.containers.stop(
                container.id,
                reason=StopContainerReason.Scheduler,
                only_if_pending=True,
            )
            if stopped.status is ContainerStatus.Stopped:
                failed.append(container.id)

        confirmations = self.confirmations
        active = self.containers.list(
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
                ttl_seconds=int(self.confirmation_seconds * 10),
            )
            if (current_time - observed_at).total_seconds() < self.confirmation_seconds:
                continue
            # Removing the record is the claim. Another scheduler that reached the
            # same conclusion finds it gone and leaves the container alone, so it
            # is failed once rather than once per scheduler.
            if not confirmations.claim_confirmed(container.id):
                continue
            if container.runtime_worker_id or container.status is ContainerStatus.Running:
                self.containers.stop(container.id, reason=StopContainerReason.Scheduler)
            else:
                if not request_service.failure_handler.mark_scheduling_failed(
                    container.id,
                    workspace_id=container.workspace_id,
                    reason=ORPHANED_CONTAINER_FAILURE_REASON,
                    now=current_time,
                ):
                    continue
                request_service.containers.delete_container_state(container.id)
                orphaned_container_networks = self.networks
                orphaned_container_networks.remove_container_ips(container.id)
            failed.append(container.id)
        return failed
