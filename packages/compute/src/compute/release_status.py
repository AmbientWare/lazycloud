from dataclasses import dataclass

from database.repositories.worker_releases import WorkerReleaseRepository
from shared.compute_fleet import MachineLifecycle
from shared.releases import (
    ActiveRelease,
    FleetReleaseStatus,
    ReleaseMachinePhase,
    ReleaseMachineStatus,
)
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from database import DatabaseClient


@dataclass(slots=True)
class ComputeReleaseStatusService:
    database: DatabaseClient

    def read(
        self, release: ActiveRelease, workers: list[SchedulerWorkerRecord]
    ) -> FleetReleaseStatus:
        with self.database.session() as session:
            observations = WorkerReleaseRepository(session).fleet_observations()
            pending_capacity = WorkerReleaseRepository(session).pending_capacity_owners()
        live = {worker.machine_id: worker for worker in workers}
        machines: list[ReleaseMachineStatus] = []
        now = utc_now()
        for observation in observations:
            worker = live.get(observation.machine_id)
            stopped = observation.provider_status == "stopped"
            image = (
                worker.runtime_image
                if worker is not None and not stopped
                else observation.runtime_image
            )
            agent = (
                worker.agent_binary_sha256
                if worker is not None and not stopped
                else observation.agent_sha256
            )
            current = release.admits(image, agent)
            compatible = release.target.accepts(image, agent)
            accepting = bool(
                worker is not None
                and not stopped
                and not observation.update_generation
                and observation.lifecycle is not MachineLifecycle.Failed
                and compatible
                and worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
            )
            reason = observation.replacement_reason
            if observation.lifecycle is MachineLifecycle.Failed:
                phase = ReleaseMachinePhase.Blocked
                reason = "failed enrolled machine requires recovery or removal"
            elif observation.update_error and observation.update_generation == release.generation:
                phase = ReleaseMachinePhase.Blocked
                reason = observation.update_error
            elif stopped:
                phase = (
                    ReleaseMachinePhase.Current if current else ReleaseMachinePhase.PendingReserve
                )
                reason = "" if current else "reserve must refresh before serving this release"
            elif observation.provider_status in {"preparing", "stopping"}:
                phase = ReleaseMachinePhase.PreparingReserve
                reason = "waiting for verified preparation and provider stop"
            elif current and accepting and not observation.update_generation:
                phase = ReleaseMachinePhase.Current
                reason = ""
            elif worker is None:
                phase = ReleaseMachinePhase.Offline
                reason = "agent must reconnect and update before accepting work"
            elif current:
                phase = ReleaseMachinePhase.Verifying
                reason = "waiting for fresh request intake and update completion"
            elif observation.update_generation:
                phase = (
                    ReleaseMachinePhase.Draining
                    if worker.status is SchedulerWorkerStatus.Draining
                    else ReleaseMachinePhase.Updating
                )
                reason = "waiting for existing work to drain and the target runtime to register"
            else:
                phase = ReleaseMachinePhase.WaitingForCapacity
                reason = reason or "waiting for update admission and replacement capacity"
            machines.append(
                ReleaseMachineStatus(
                    machine_id=observation.machine_id,
                    worker_id=worker.worker_id if worker is not None else observation.worker_id,
                    capacity_owner_id=observation.capacity_owner_id,
                    phase=phase,
                    current=current,
                    compatible=compatible,
                    accepting_work=accepting,
                    reason=reason,
                )
            )
        return FleetReleaseStatus(
            release=release,
            complete=not pending_capacity
            and all(machine.phase is ReleaseMachinePhase.Current for machine in machines),
            machines=machines,
            pending_capacity_owners=pending_capacity,
        )
