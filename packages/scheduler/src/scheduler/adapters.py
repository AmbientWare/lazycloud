from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from control.service import ControlPlaneService
from database.records.apps import AutoscalingStubRecord, StubRecord
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.orchestration import ContainerRepository
from shared.containers import ContainerStatus
from shared.identity import WorkspaceRecord
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
class SchedulerWorkloadDirectoryAdapter:
    control_plane: ControlPlaneService

    def list_stubs(self, *, workspace: str | None = None) -> list[StubRecord]:
        return self.control_plane.list_stubs(workspace=workspace)

    def list_autoscaling_stubs(
        self,
        stub_ids: Sequence[str] | None = None,
    ) -> list[AutoscalingStubRecord]:
        return self.control_plane.list_autoscaling_stubs(stub_ids)

    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord:
        return self.control_plane.get_stub(stub_id_or_name, workspace=workspace)

    def get_workspace(self, workspace: str = "default") -> WorkspaceRecord:
        return self.control_plane.get_workspace(workspace)

    def set_autoscaling_enabled(
        self,
        stub_id_or_name: str,
        *,
        workspace: str,
        enabled: bool,
    ) -> StubRecord:
        return self.control_plane.update_stub_config(
            stub_id_or_name,
            workspace=workspace,
            fields={"metadata.autoscaling_enabled": enabled},
        ).stub
