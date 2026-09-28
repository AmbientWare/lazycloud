from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from database.repositories.compute import ComputeUnitRepository
from shared.http.fleet import (
    FleetCapacityResponse,
    FleetMarketResponse,
    FleetNodeListResponse,
    FleetNodeResponse,
    FleetPlanResponse,
    FleetReleaseResponse,
    FleetStateResponse,
    FleetSummaryResponse,
)
from shared.releases import ActiveRelease
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.timestamps import utc_now

from compute.fleet_reserves import machine_capacity, reserve_machine
from compute.release_status import ComputeReleaseStatusService
from compute.reserve_state import FleetReserveState
from database import DatabaseClient


@dataclass(slots=True)
class FleetStatusService:
    database: DatabaseClient
    reserves: FleetReserveState

    def summary(
        self, release: ActiveRelease | None, workers: list[SchedulerWorkerRecord]
    ) -> FleetSummaryResponse:
        now = utc_now()
        publication = self.reserves.published()
        plan = None
        if (
            publication is not None
            and release is not None
            and publication.release == release.target
        ):
            plan = FleetPlanResponse(
                generated_at=publication.generated_at,
                expires_at=publication.expires_at,
                markets=[
                    FleetMarketResponse(
                        preemptible=market.market.preemptible,
                        gpu_type=market.market.gpu_type,
                        warm_free=FleetCapacityResponse.model_validate(market.warm_free),
                        warm_target=FleetCapacityResponse.model_validate(market.warm_target),
                        reserve_ready=FleetCapacityResponse.model_validate(market.stopped_ready),
                        reserve_target=FleetCapacityResponse.model_validate(market.stopped_target),
                        allocated=FleetCapacityResponse.model_validate(market.load),
                        states=[
                            FleetStateResponse(
                                state=state,
                                machines=observed.machines,
                                capacity=FleetCapacityResponse.model_validate(observed.capacity),
                                allocated=FleetCapacityResponse.model_validate(observed.allocated),
                            )
                            for state, observed in market.observed.items()
                        ],
                        reason=market.application_reason or market.reason,
                    )
                    for market in publication.markets.values()
                ],
            )
        rollout = None
        if release is not None:
            status = ComputeReleaseStatusService(self.database).read(release, workers)
            rollout = FleetReleaseResponse(
                version=release.target.version,
                generation=release.generation,
                complete=status.complete,
                phases=dict(Counter(machine.phase for machine in status.machines)),
                pending_capacity_owners=len(status.pending_capacity_owners),
            )
        return FleetSummaryResponse(observed_at=now, plan=plan, release=rollout)

    def nodes(
        self,
        release: ActiveRelease | None,
        workers: list[SchedulerWorkerRecord],
        *,
        cursor: UUID | None,
        limit: int,
    ) -> FleetNodeListResponse:
        now = utc_now()
        ready = frozenset(
            worker.machine_id
            for worker in workers
            if release is not None
            and release.target.accepts(worker.runtime_image, worker.agent_binary_sha256)
            and worker.request_intake_status(at=now) is SchedulerWorkerStatus.Available
        )
        with self.database.session() as session:
            rows = ComputeUnitRepository(session).platform_reserve_rows(
                after_instance_id=cursor, limit=limit + 1
            )
        units = {unit.id: unit for unit in rows.units}
        nodes: list[FleetNodeResponse] = []
        for instance in rows.instances[:limit]:
            unit = units[instance.unit_id]
            machine = reserve_machine(
                instance,
                unit.billing_minimum_seconds,
                now=now,
                ready_machine_ids=ready,
                release=release,
            )
            if machine is None:
                continue
            nodes.append(
                FleetNodeResponse(
                    id=instance.record_id,
                    machine_id=instance.machine_id,
                    instance_id=instance.instance_id,
                    provider=unit.provider,
                    region=unit.region,
                    instance_type=instance.instance_type,
                    preemptible=unit.preemptible,
                    gpu_type=unit.gpu_type,
                    state=machine.state,
                    capacity=FleetCapacityResponse.model_validate(
                        machine_capacity(
                            unit.cpu_millicores,
                            unit.memory_mib,
                            unit.gpu_count,
                            reported_memory_mib=unit.reported_memory_mib,
                        )
                    ),
                    allocated=FleetCapacityResponse.model_validate(machine.load),
                    containers=machine.containers,
                    ready=machine.ready,
                )
            )
        return FleetNodeListResponse(
            data=nodes,
            next=rows.instances[limit - 1].record_id if len(rows.instances) > limit else "",
            observed_at=now,
        )
