from dataclasses import dataclass

from control.service import ControlPlaneService
from database.records.apps import StubRecord
from database.repositories.orchestration import ContainerRepository
from execution.endpoints.dispatch import ACTIVE_ENDPOINT_DISPATCH_STATUSES
from execution.endpoints.service import EndpointDispatchStateRepository
from scheduler.autoscaling import EndpointAutoscalingDispatchObservation
from shared.containers import ContainerStatus
from shared.identity import WorkspaceRecord

from database import DatabaseClient


@dataclass(frozen=True, slots=True)
class EndpointDispatchAutoscalingReader:
    repository: EndpointDispatchStateRepository

    def active_count(self, stub_id: str) -> int:
        return self.repository.active_count(stub_id)

    def list_by_stub(self, stub_id: str) -> list[EndpointAutoscalingDispatchObservation]:
        return [
            EndpointAutoscalingDispatchObservation(
                container_id=record.container_id,
                active=record.status in ACTIVE_ENDPOINT_DISPATCH_STATUSES,
                finished_at=record.finished_at,
            )
            for record in self.repository.list_by_stub(stub_id)
        ]


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
