from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from compute.agent_control import MachineWorkerAvailability, agent_machine_worker_id
from compute.offers import ComputeOffer
from compute.state import ComputeUnitState, ComputeUnitStatus
from coordination.process_presence import ProcessPresenceReader
from shared.compute_policy import ComputeUnitPhase, ComputeUnitRecord
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus, WorkerUnavailableReason
from shared.timestamps import utc_now


class ComputeUnitStateRepository(Protocol):
    def save_unit_state(self, state: ComputeUnitState) -> ComputeUnitState: ...

    def delete_agent_machine_state_for_machine(
        self,
        workspace_id: str,
        machine_id: str,
    ) -> bool: ...

    def revoke_join_token_state(self, token_hash: str) -> bool: ...


class SchedulerHookWorkerRepository(Protocol):
    def list_workers(self) -> list[SchedulerWorkerRecord]: ...

    def get_worker(self, worker_id: str) -> SchedulerWorkerRecord | None: ...

    def disable_worker(
        self,
        worker_id: str,
        *,
        reason: WorkerUnavailableReason,
        detail: str = "",
        ttl_seconds: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWorkerRecord: ...


@dataclass(slots=True)
class SchedulerComputeHooks:
    compute_states: ComputeUnitStateRepository
    workers: SchedulerHookWorkerRepository
    agent_intake: ProcessPresenceReader | None = None

    def register_internal_unit(self, unit: ComputeUnitRecord, offer: ComputeOffer) -> None:
        self.compute_states.save_unit_state(_internal_compute_unit_state(unit, offer))

    def disable_machine(self, machine_id: str, reason: str) -> None:
        for worker in self._workers_for_machine(machine_id):
            self.workers.disable_worker(
                worker.worker_id,
                reason=WorkerUnavailableReason.MachineRetired,
                detail=reason,
            )

    def retire_machine(
        self,
        workspace_id: str,
        machine_id: str,
        reason: str,
    ) -> None:
        self.disable_machine(machine_id, reason)
        self.compute_states.delete_agent_machine_state_for_machine(workspace_id, machine_id)

    def revoke_unit_join_token(self, token_hash: str) -> None:
        self.compute_states.revoke_join_token_state(token_hash)

    def machine_worker_availability(self, machine_id: str) -> MachineWorkerAvailability:
        """What the hot record says about this machine's worker, including nothing.

        An absent record is `Unknown`, not `Unavailable`. The key carries a short
        TTL that a worker re-arms as it reports, so its absence means we have not
        heard rather than that the worker refused, and the reclaim terminates
        billable machines on the difference.
        """
        worker = self.workers.get_worker(agent_machine_worker_id(machine_id))
        if worker is None:
            return MachineWorkerAvailability.Unknown
        if worker.status is SchedulerWorkerStatus.Draining:
            return MachineWorkerAvailability.Unknown
        if worker.status is SchedulerWorkerStatus.Available:
            return (
                MachineWorkerAvailability.Available
                if worker.request_intake_status(at=utc_now()) is SchedulerWorkerStatus.Available
                else MachineWorkerAvailability.Unknown
            )
        return MachineWorkerAvailability.Unavailable

    def agent_intake_observing_since(self) -> datetime | None:
        if self.agent_intake is None:
            return None
        return self.agent_intake.observing_since()

    def _workers_for_machine(self, machine_id: str) -> list[SchedulerWorkerRecord]:
        workers = [
            worker
            for worker in self.workers.list_workers()
            if worker.machine_id == machine_id
            and worker.status is not SchedulerWorkerStatus.Unavailable
        ]
        if workers:
            return workers
        worker = self.workers.get_worker(agent_machine_worker_id(machine_id))
        if worker is None or worker.status is SchedulerWorkerStatus.Unavailable:
            return []
        return [worker]


def _internal_compute_unit_state(
    pool: ComputeUnitRecord,
    offer: ComputeOffer,
) -> ComputeUnitState:
    return ComputeUnitState(
        workspace_id=pool.workspace_id,
        name=pool.name,
        pool=pool.pool,
        capacity_owner_id=pool.capacity_owner_id,
        platform_fleet=pool.platform_fleet,
        default_eligible=pool.default_eligible,
        provider=pool.provider_ref,
        status=_internal_pool_status(pool.phase),
        min_machines=pool.min_machines,
        max_machines=pool.max_machines,
        desired_machines=pool.desired_machines,
        active_machines=pool.observed_machines,
        metadata={
            "capacity_owner_kind": pool.capacity_owner_kind.value,
            "capacity_owner_source": pool.capacity_owner_source.value,
            "source": pool.source,
            "selector": pool.selector,
            "capacity_mode": pool.capacity_mode.value,
            "visibility": pool.visibility.value,
            "provider_connection_id": pool.provider_connection_id,
            "region": pool.region,
            "offer_id": pool.offer_id,
            "capability_key": pool.capability_key,
            "config": {
                "name": pool.name,
                "selector": pool.selector,
                "providers": [pool.provider_ref],
                "regions": [pool.region],
                "offer_id": pool.offer_id,
            },
            "sizing": {
                "default_worker_cpu": offer.cpu_millicores / 1000,
                "default_worker_memory": offer.memory_mb,
                "default_worker_gpu_type": offer.gpu or "",
                "default_worker_gpu_count": offer.gpu_count,
            },
            "drain": {
                "scale_down_enabled": "true",
                "scale_down_idle_seconds": pool.config.get(
                    "idle_timeout_seconds",
                    300,
                ),
            },
        },
    )


def _internal_pool_status(phase: ComputeUnitPhase) -> ComputeUnitStatus:
    if phase is ComputeUnitPhase.Deleted:
        return ComputeUnitStatus.Deleted
    if phase is ComputeUnitPhase.Deleting:
        return ComputeUnitStatus.Draining
    if phase in {ComputeUnitPhase.Provisioning, ComputeUnitPhase.Degraded}:
        return ComputeUnitStatus.Pending
    return ComputeUnitStatus.Active
