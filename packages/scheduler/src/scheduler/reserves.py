"""The scheduler's half of the fleet reserve: free headroom, and consolidation.

Compute plans each market's headroom. The scheduler reports what its workers
still have free, so a sustained shortfall brings the next plan forward, and
empties the lightly used machine a plan names.

A workload may be moved if and only if it is preemptible, whatever its kind:
being preemptible is how it accepted being stopped and rescheduled. Functions,
endpoints, image builds, pods, devboxes and sandboxes on those terms may move;
nothing else is ever stopped here. Movable work stops as a preemption, so its
retries and billing treat it as one. Image builds finish where they are, since
a build restarted elsewhere repeats its work. Once the machine holds nothing,
the idle drain retires it or returns it to the stopped reserve like any idle
machine.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from compute.fleet_policy import Capacity, FleetReservePlan, ReserveMarket
from compute.fleet_reserves import unit_reserve_market
from compute.reserve_state import Consolidation, FleetReserveState
from database.repositories.orchestration import MachineContainer
from shared.container_requests import StopContainerReason
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from scheduler.containers import scheduling_request, worker_capacity
from scheduler.preemption import WorkerPlannedDrainOperation, WorkerPreemptionQueueResult
from scheduler.tools import select_worker_for_request

LOGGER = logging.getLogger(__name__)

CONSOLIDATION_REASON = "consolidating onto the rest of the fleet"


def reserve_free_capacity(
    workers: Sequence[SchedulerWorkerRecord], *, now: datetime
) -> dict[ReserveMarket, Capacity]:
    """What each platform market's serving workers can still take."""
    free: dict[ReserveMarket, Capacity] = {}
    for worker in workers:
        if worker.placement != Placement.platform() or worker.request_intake_status(at=now) not in {
            SchedulerWorkerStatus.Available,
            SchedulerWorkerStatus.Pending,
        }:
            continue
        market = unit_reserve_market(preemptible=worker.preemptible, gpu_type=worker.gpu_type)
        free[market] = free.get(market, Capacity()) + Capacity(
            worker.free_cpu_millicores, worker.free_memory_mib, worker.free_gpu_count
        )
    return free


class ConsolidationCompute(Protocol):
    def provider_machine_unit(self, machine_id: str) -> tuple[str, str] | None: ...

    def drain_internal_unit_machine(
        self, workspace_id: str, machine_id: str, *, reason: str, now: datetime | None = None
    ) -> bool: ...


class ConsolidationContainers(Protocol):
    def live_on_machine(self, machine_id: str) -> list[MachineContainer]: ...


class ConsolidationWorkers(Protocol):
    def list_workers(self) -> list[SchedulerWorkerRecord]: ...

    def list_workers_on_machine(self, machine_id: str) -> list[SchedulerWorkerRecord]: ...

    def drain_worker_for_maintenance(
        self, operation: WorkerPlannedDrainOperation, *, now: datetime
    ) -> WorkerPreemptionQueueResult: ...


class ConsolidationStopper(Protocol):
    def stop(self, container_id: str, *, reason: StopContainerReason) -> object: ...


class ConsolidationLeases(Protocol):
    def dispatch_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...


@dataclass(slots=True)
class FleetConsolidationService:
    compute: ConsolidationCompute
    containers: ConsolidationContainers
    workers: ConsolidationWorkers
    stopper: ConsolidationStopper
    leases: ConsolidationLeases
    state: FleetReserveState
    cooldown_seconds: int
    deadline_seconds: int
    """How long a consolidation may take before it is given up and the machine left
    to drain on its own."""

    restop_seconds: int = 300

    def reconcile(self, plan: FleetReservePlan | None, *, now: datetime | None = None) -> None:
        current_time = now or utc_now()
        if plan is not None:
            for market in plan.markets:
                if market.consolidate:
                    self.begin(market.market, market.consolidate, now=current_time)
        for market, consolidation in self.state.consolidations().items():
            self.advance(market, consolidation, now=current_time)

    def begin(self, market: ReserveMarket, machine_id: str, *, now: datetime) -> bool:
        """Cordon the machine if everything on it may move and has a host to move to.

        The durable rows are read again under the unit's dispatch lock, so work
        placed after the planner looked is seen, and nothing is placed here once
        the lock is released.
        """
        owner = self.compute.provider_machine_unit(machine_id)
        if owner is None:
            return False
        unit_id, workspace_id = owner
        consolidation = Consolidation(
            machine_id=machine_id, unit_id=unit_id, workspace_id=workspace_id, started_at=now
        )
        if not self.state.begin_consolidation(market, consolidation, ttl_seconds=self._ttl):
            return False
        cordoned = False
        try:
            with self.leases.dispatch_lock(unit_id):
                live = self.containers.live_on_machine(machine_id)
                refusal = self._refusal(machine_id, live, now=now)
                if not refusal and not self.compute.drain_internal_unit_machine(
                    workspace_id, machine_id, reason=CONSOLIDATION_REASON, now=now
                ):
                    refusal = "it is already draining"
                if refusal:
                    self.state.finish_consolidation(market, cooldown_seconds=0)
                    LOGGER.info("not consolidating %s: %s", machine_id, refusal)
                    return False
                cordoned = True
                self._drain_workers(machine_id, now=now)
        except Exception:
            # A cordoned machine keeps its consolidation, so its work still moves.
            if not cordoned:
                self.state.finish_consolidation(market, cooldown_seconds=0)
            raise
        LOGGER.info(
            "consolidating %s in %s: moving %d containers", machine_id, market.key, len(live)
        )
        return True

    def advance(
        self, market: ReserveMarket, consolidation: Consolidation, *, now: datetime
    ) -> None:
        live = self.containers.live_on_machine(consolidation.machine_id)
        if not live:
            self.state.finish_consolidation(market, cooldown_seconds=self.cooldown_seconds)
            LOGGER.info("consolidated %s; the idle drain releases it", consolidation.machine_id)
            return
        if (now - consolidation.started_at).total_seconds() >= self.deadline_seconds:
            # The machine stays cordoned, so what is left finishes and it drains.
            self.state.finish_consolidation(market, cooldown_seconds=self.cooldown_seconds)
            LOGGER.warning(
                "consolidating %s gave up with %d containers left",
                consolidation.machine_id,
                len(live),
            )
            return
        if (
            consolidation.stopped_at is not None
            and (now - consolidation.stopped_at).total_seconds() < self.restop_seconds
        ):
            return
        # Stopping an already stopping container changes nothing, so a stop that
        # never arrived is sent again rather than waited on.
        for container in _movable(live):
            self.stopper.stop(container.id, reason=StopContainerReason.Preempted)
        self.state.save_consolidation(
            market, consolidation.model_copy(update={"stopped_at": now}), ttl_seconds=self._ttl
        )

    @property
    def _ttl(self) -> int:
        return self.deadline_seconds * 2

    def _refusal(self, machine_id: str, live: Sequence[MachineContainer], *, now: datetime) -> str:
        if not live:
            return "it emptied"
        movable = _movable(live)
        if any(container.pinned for container in live) or any(
            container.request is None for container in movable
        ):
            return "it holds work that cannot move"
        hosts = {
            worker.worker_id: worker_capacity(worker, now=now)
            for worker in self.workers.list_workers()
            if worker.machine_id != machine_id
            and worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
        }
        # Largest first, each onto the host placement itself would choose, so
        # every container has a specific machine with its CPU, memory, cards and
        # disks free before any of them is stopped.
        requests = sorted(
            (
                scheduling_request(container.request, owner_user_id="", provisionable=False)
                for container in movable
                if container.request is not None
            ),
            key=lambda request: (request.cpu, request.memory_mib, request.gpu_count),
            reverse=True,
        )
        for request in requests:
            host = select_worker_for_request(request, hosts.values())
            if host is None:
                return f"no other machine has room for container {request.id}"
            hosts[host.worker_id] = host.reserve(request)
        return ""

    def _drain_workers(self, machine_id: str, *, now: datetime) -> None:
        for worker in self.workers.list_workers_on_machine(machine_id):
            self.workers.drain_worker_for_maintenance(
                WorkerPlannedDrainOperation(
                    operation_id=f"consolidate:{machine_id}:{now.isoformat()}",
                    worker_id=worker.worker_id,
                    capacity_owner_id=worker.capacity_owner_id,
                    machine_id=machine_id,
                    expected_resource_version=worker.resource_version,
                    reason=CONSOLIDATION_REASON,
                    observed_at=now,
                ),
                now=now,
            )


def _movable(live: Sequence[MachineContainer]) -> list[MachineContainer]:
    return [container for container in live if not container.pinned and not container.image_build]


__all__ = [
    "CONSOLIDATION_REASON",
    "ConsolidationCompute",
    "ConsolidationContainers",
    "ConsolidationLeases",
    "ConsolidationStopper",
    "ConsolidationWorkers",
    "FleetConsolidationService",
    "reserve_free_capacity",
]
