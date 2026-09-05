from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shared.containers import ContainerRecord
from shared.scheduling import (
    ContainerSchedulingDirectory,
    SchedulerContainerAddress,
    SchedulerContainerAddressMap,
    WorkerContainerState,
)

from worker.container_client.control import (
    ContainerServiceClient,
    ContainerServiceTransport,
    plan_container_client_connection_options,
)
from worker.container_client.models import ContainerClientConnectionOptions


class ContainerServiceTransportFactory(Protocol):
    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport: ...


@dataclass(frozen=True, slots=True)
class SchedulerContainerClient:
    client: ContainerServiceClient
    state: WorkerContainerState
    worker_address: SchedulerContainerAddress
    options: ContainerClientConnectionOptions


@dataclass(slots=True)
class SchedulerContainerClientFactory:
    scheduler_containers: ContainerSchedulingDirectory | None = None
    transport_factory: ContainerServiceTransportFactory | None = None
    service_token: str = ""

    def client_for(self, container: ContainerRecord) -> SchedulerContainerClient:
        repository = self._repository()
        state = repository.get_container_state(container.id)
        if state is None:
            msg = f"scheduler state not found for container {container.id}"
            raise RuntimeError(msg)
        if state.workspace_id != container.workspace_id:
            msg = "container scheduler state workspace mismatch"
            raise RuntimeError(msg)
        worker_address = repository.get_worker_address(container.id)
        if worker_address is None or not worker_address.address:
            msg = f"worker address not published for container {container.id}"
            raise RuntimeError(msg)
        transport_factory = self.transport_factory
        if transport_factory is None:
            msg = "container service transport factory is not configured"
            raise RuntimeError(msg)
        options = plan_container_client_connection_options(
            worker_address.address,
            self.service_token,
            backend_route_id=(
                worker_address.route.route_id if worker_address.route is not None else ""
            ),
        )
        return SchedulerContainerClient(
            client=ContainerServiceClient(transport_factory.create_transport(options)),
            state=state,
            worker_address=worker_address,
            options=options,
        )

    def state_for(self, container: ContainerRecord) -> WorkerContainerState | None:
        repository = self.scheduler_containers
        if repository is None:
            return None
        state = repository.get_container_state(container.id)
        if state is None or state.workspace_id != container.workspace_id:
            return None
        return state

    def address_map_for(self, container_id: str) -> SchedulerContainerAddressMap:
        return self._repository().get_container_address_map(container_id)

    def _repository(self) -> ContainerSchedulingDirectory:
        if self.scheduler_containers is None:
            msg = "scheduler container repository is not configured"
            raise RuntimeError(msg)
        return self.scheduler_containers


@dataclass(slots=True)
class SchedulerContainerServiceStopper:
    client_factory: SchedulerContainerClientFactory

    def stop_container(self, container: ContainerRecord) -> None:
        response = self.client_factory.client_for(container).client.kill(container.id)
        if not response.ok:
            msg = response.error_msg or f"failed to stop container {container.id}"
            raise RuntimeError(msg)


__all__ = [
    "ContainerServiceTransportFactory",
    "SchedulerContainerClient",
    "SchedulerContainerClientFactory",
    "SchedulerContainerServiceStopper",
]
