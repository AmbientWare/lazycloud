from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.orchestration import ContainerRepository, MachineContainer
from shared.containers import ContainerStatus
from shared.timestamps import utc_now

from database import DatabaseClient
from scheduler.autoscaling import EndpointAutoscalingDispatchObservation


@dataclass(frozen=True, slots=True)
class EndpointDispatchAutoscalingReader:
    database: DatabaseClient

    def active_counts_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]:
        with self.database.session() as session:
            return EndpointDispatchRepository(session).active_counts_by_stub(stub_ids, at=utc_now())

    def observations_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        finished_since: datetime,
    ) -> dict[str, list[EndpointAutoscalingDispatchObservation]]:
        with self.database.session() as session:
            observations = EndpointDispatchRepository(session).observations_by_stub(
                stub_ids, at=utc_now(), finished_since=finished_since
            )
        return {
            stub_id: [
                EndpointAutoscalingDispatchObservation(
                    container_id=record.container_id,
                    active=record.active,
                    finished_at=record.finished_at,
                )
                for record in records
            ]
            for stub_id, records in observations.items()
        }


@dataclass(frozen=True, slots=True)
class DatabaseCapacityAllocationOwners:
    database: DatabaseClient

    def is_active(self, *, workspace_id: str, container_id: str) -> bool:
        with self.database.session() as session:
            container = ContainerRepository(session).get(
                container_id,
                workspace_id=workspace_id,
            )
        return container is not None and container.status in {
            ContainerStatus.Pending,
            ContainerStatus.Running,
        }


@dataclass(frozen=True, slots=True)
class DatabaseMachineContainers:
    database: DatabaseClient

    def live_on_machine(self, machine_id: str) -> list[MachineContainer]:
        with self.database.session() as session:
            return ContainerRepository(session).live_on_machine(machine_id)
