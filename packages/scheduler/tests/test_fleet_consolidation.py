from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from compute.fleet_resources import Capacity, ReserveMarket
from compute.reserve_state import Consolidation, RedisFleetReserveState
from database.context import ServiceContext
from database.mappers.containers import write_scheduling_request
from database.repositories.orchestration import ContainerRepository
from database.tables.orchestration import ContainerTable
from scheduler.adapters import DatabaseMachineContainers
from scheduler.preemption import WorkerPlannedDrainOperation, WorkerPreemptionQueueResult
from scheduler.reserves import FleetConsolidationService
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.placement import Placement
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerRequest, SchedulerWorkerStatus
from tests.real_redis import RealRedisActors

MARKET = ReserveMarket(preemptible=False)
UNIT_ID = "00000000-0000-4000-8000-00000000c0de"


@dataclass(slots=True)
class _Fleet:
    hosts: list[SchedulerWorkerRecord] = field(default_factory=list)
    drained: dict[str, str] = field(default_factory=dict)
    stopped: list[tuple[str, StopContainerReason]] = field(default_factory=list)

    def provider_machine_unit(self, machine_id: str) -> tuple[str, str] | None:
        return UNIT_ID, "workspace"

    def drain_internal_unit_machine(
        self, workspace_id: str, machine_id: str, *, reason: str, now: datetime | None = None
    ) -> bool:
        self.drained[machine_id] = reason
        return True

    def internal_unit_machine_drain_reason(self, workspace_id: str, machine_id: str) -> str | None:
        return self.drained.get(machine_id)

    def list_workers(self) -> list[SchedulerWorkerRecord]:
        return self.hosts

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
        yield


def _host(machine_id: str, *, free_cpu_millicores: int) -> SchedulerWorkerRecord:
    return SchedulerWorkerRecord(
        worker_id=f"worker-{machine_id}",
        machine_id=machine_id,
        capacity_owner_id=UNIT_ID,
        placement=Placement.platform(),
        status=SchedulerWorkerStatus.Available,
        request_poll_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        free_cpu_millicores=free_cpu_millicores,
        free_memory_mib=16_384,
        total_cpu_millicores=8_000,
        total_memory_mib=16_384,
    )


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
        write_scheduling_request(
            row,
            SchedulerWorkerRequest(
                workspace_id=workspace_id,
                stub_id="stub",
                container_id=container_id,
                placement=Placement.platform(),
                cpu_millicores=2_000,
                memory_mib=2_048,
                preemptible=preemptible,
            ),
        )
    return container_id


def test_consolidation_moves_only_preemptible_work_after_rechecking_under_the_lock(
    service_context: ServiceContext, real_redis_actors: RealRedisActors
) -> None:
    fleet = _Fleet(hosts=[_host("roomy", free_cpu_millicores=4_000)])
    service = FleetConsolidationService(
        compute=fleet,
        containers=DatabaseMachineContainers(service_context.database),
        workers=fleet,
        stopper=fleet,
        leases=fleet,
        state=RedisFleetReserveState(real_redis_actors.client()),
        cooldown_seconds=0,
        deadline_seconds=3_600,
    )
    now = datetime.now(UTC)

    light = str(uuid4())
    function = _place(service_context, light, preemptible=True, name="function")
    devbox = _place(service_context, light, preemptible=True, name="devbox")
    assert service.begin(
        MARKET, light, destination_machine_ids=("roomy",), warm_target=Capacity(), now=now
    )
    assert service.state.consolidations()[MARKET].destination_machine_ids == ("roomy",)
    for market, consolidation in service.state.consolidations().items():
        service.advance(market, consolidation, now=now)
    assert sorted(fleet.stopped) == sorted(
        [(function, StopContainerReason.Preempted), (devbox, StopContainerReason.Preempted)]
    )

    # Free CPU split across two machines holds neither container whole.
    service.state.finish_consolidation(MARKET, cooldown_seconds=0)
    fleet.stopped.clear()
    fleet.hosts = [
        _host("left", free_cpu_millicores=1_500),
        _host("right", free_cpu_millicores=1_500),
    ]
    assert not service.begin(
        MARKET, light, destination_machine_ids=("left", "right"), warm_target=Capacity(), now=now
    )
    assert service.state.consolidations() == {}
    fleet.hosts = [_host("roomy", free_cpu_millicores=4_000)]

    # Work that did not accept interruption landed after the planner looked;
    # the read under the lock sees it, and nothing is cordoned or stopped.
    service.state.finish_consolidation(MARKET, cooldown_seconds=0)
    fleet.stopped.clear()
    pinned = str(uuid4())
    _place(service_context, pinned, preemptible=True, name="pod")
    _place(service_context, pinned, preemptible=False, name="devbox")
    assert not service.begin(
        MARKET, pinned, destination_machine_ids=("roomy",), warm_target=Capacity(), now=now
    )
    assert fleet.stopped == []
    assert service.state.consolidations() == {}

    uncordoned = str(uuid4())
    _place(service_context, uncordoned, preemptible=True, name="function")
    interrupted = Consolidation(
        machine_id=uncordoned,
        unit_id=UNIT_ID,
        workspace_id="workspace",
        started_at=now,
        destination_machine_ids=("roomy",),
    )
    assert service.state.begin_consolidation(MARKET, interrupted, ttl_seconds=3600)
    service.advance(MARKET, interrupted, now=now)
    assert not fleet.stopped
    assert not service.state.consolidations()

    assert service.begin(
        MARKET, uncordoned, destination_machine_ids=("roomy",), warm_target=Capacity(), now=now
    )
    fleet.hosts = [_host("unreserved", free_cpu_millicores=8_000)]
    service.advance(MARKET, service.state.consolidations()[MARKET], now=now)
    assert not fleet.stopped
