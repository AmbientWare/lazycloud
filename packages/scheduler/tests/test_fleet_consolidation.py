from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from compute.fleet_policy import ReserveMarket
from compute.reserve_state import RedisFleetReserveState
from database.context import ServiceContext
from database.repositories.orchestration import ContainerRepository, MachineContainer
from database.tables.orchestration import ContainerTable
from scheduler.adapters import DatabaseMachineContainers
from scheduler.preemption import WorkerPlannedDrainOperation, WorkerPreemptionQueueResult
from scheduler.reserves import FleetConsolidationService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRecord
from tests.real_redis import RealRedisActors

MARKET = ReserveMarket(preemptible=False)
UNIT_ID = "00000000-0000-4000-8000-00000000c0de"


@dataclass(slots=True)
class _Fleet:
    events: list[str] = field(default_factory=list)
    stopped: list[tuple[str, StopContainerReason]] = field(default_factory=list)

    def provider_machine_unit(self, machine_id: str) -> tuple[str, str] | None:
        return UNIT_ID, "workspace"

    def drain_internal_unit_machine(
        self, workspace_id: str, machine_id: str, *, reason: str, now: datetime | None = None
    ) -> bool:
        self.events.append(f"cordon {machine_id}")
        return True

    def list_workers_on_machine(self, machine_id: str) -> list[SchedulerWorkerRecord]:
        return []

    def drain_worker_for_maintenance(
        self, operation: WorkerPlannedDrainOperation, *, now: datetime
    ) -> WorkerPreemptionQueueResult:
        raise AssertionError("the machine has no registered worker")

    def stop(self, container_id: str, *, reason: StopContainerReason) -> object:
        self.stopped.append((container_id, reason))
        return None

    @contextmanager
    def dispatch_lock(self, capacity_owner_id: str) -> Iterator[None]:
        self.events.append("lock")
        try:
            yield
        finally:
            self.events.append("unlock")


@dataclass(slots=True)
class _Containers:
    database: DatabaseMachineContainers
    events: list[str]

    def live_on_machine(self, machine_id: str) -> list[MachineContainer]:
        self.events.append(f"read {machine_id}")
        return self.database.live_on_machine(machine_id)


def _place(context: ServiceContext, machine_id: str, *, preemptible: bool, name: str) -> str:
    with context.database.session() as session:
        workspace_id = context.default_workspace_id(session)
        container_id = str(uuid4())
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name=name,
                image="image",
                command=[],
                workspace_id=workspace_id,
                status=ContainerStatus.Running,
            )
        )
        row = session.get(ContainerTable, container_id)
        assert row is not None
        row.runtime_machine_id = machine_id
        row.scheduling_placement = Placement.platform().key
        row.scheduling_preemptible = preemptible
    return container_id


def test_consolidation_moves_only_preemptible_work_after_rechecking_under_the_lock(
    service_context: ServiceContext, real_redis_actors: RealRedisActors
) -> None:
    fleet = _Fleet()
    service = FleetConsolidationService(
        compute=fleet,
        containers=_Containers(DatabaseMachineContainers(service_context.database), fleet.events),
        workers=fleet,
        stopper=fleet,
        leases=fleet,
        state=RedisFleetReserveState(real_redis_actors.client()),
        cooldown_seconds=0,
    )
    now = datetime.now(UTC)

    light = str(uuid4())
    function = _place(service_context, light, preemptible=True, name="function")
    devbox = _place(service_context, light, preemptible=True, name="devbox")
    assert service.begin(MARKET, light, now=now)
    assert fleet.events == ["lock", f"read {light}", f"cordon {light}", "unlock"]
    for market, consolidation in service.state.consolidations().items():
        service.advance(market, consolidation)
    assert sorted(fleet.stopped) == sorted(
        [(function, StopContainerReason.Scheduler), (devbox, StopContainerReason.Scheduler)]
    )

    # Work that did not accept interruption landed after the planner looked;
    # the read under the lock sees it, and nothing is cordoned or stopped.
    service.state.finish_consolidation(MARKET, cooldown_seconds=0)
    fleet.events.clear()
    fleet.stopped.clear()
    pinned = str(uuid4())
    _place(service_context, pinned, preemptible=True, name="pod")
    _place(service_context, pinned, preemptible=False, name="devbox")
    assert not service.begin(MARKET, pinned, now=now)
    assert fleet.events == ["lock", f"read {pinned}", "unlock"]
    assert fleet.stopped == []
    assert service.state.consolidations() == {}
