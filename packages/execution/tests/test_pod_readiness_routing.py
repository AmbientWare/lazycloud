from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.repositories.orchestration import ContainerRepository
from execution.pods.planning import PodProxyProtocol
from shared.containers import ContainerRecord, ContainerStatus
from shared.routing import AgentBackendRoute, BackendRouteState
from shared.scheduling import (
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerContainerStatus,
)

SERVING_CONTAINER = "00000000-0000-4000-8000-0000000002a1"
STARTED_CONTAINER = "00000000-0000-4000-8000-0000000002a2"
PORT = 8080


@dataclass(slots=True)
class _RunningContainers:
    """Every container is scheduled, running, and has an address for the port."""

    workspace_id: str
    stub_id: str

    def _state(self, container_id: str) -> SchedulerContainerState:
        return SchedulerContainerState(
            container_id=container_id,
            workspace_id=self.workspace_id,
            stub_id=self.stub_id,
            status=SchedulerContainerStatus.Running,
        )

    def _address_map(self, container_id: str) -> SchedulerContainerAddressMap:
        return SchedulerContainerAddressMap(
            container_id=container_id,
            address_map={PORT: f"route://{container_id}"},
            routes=[
                AgentBackendRoute(
                    route_id=container_id,
                    container_id=container_id,
                    port=PORT,
                    state=BackendRouteState.Ready,
                )
            ],
        )

    async def get_container_state(self, container_id: str) -> SchedulerContainerState:
        return self._state(container_id)

    async def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]:
        assert stub_id == self.stub_id
        return [
            self._state(container_id) for container_id in (SERVING_CONTAINER, STARTED_CONTAINER)
        ]

    async def get_container_address_map(
        self,
        container_id: str,
    ) -> SchedulerContainerAddressMap:
        return self._address_map(container_id)

    async def get_container_address_maps(
        self,
        container_ids: list[str],
    ) -> dict[str, SchedulerContainerAddressMap]:
        return {container_id: self._address_map(container_id) for container_id in container_ids}


@dataclass(frozen=True, slots=True)
class _OnlyOneIsServing:
    serving_container_id: str

    async def is_ready(
        self,
        *,
        container_id: str,
        stub_id: str,
        address: str,
        route_id: str,
        port: int,
        health_path: str = "",
    ) -> bool:
        _ = stub_id, address, route_id, port, health_path
        return container_id == self.serving_container_id


@pytest.mark.anyio
async def test_pod_proxy_routes_past_a_container_whose_workload_is_not_serving(
    async_services: ApiServices,
) -> None:
    """A started container is not a serving one, and only the probe knows which.

    Both containers here are `Running` with an exposed address, which is
    everything routing looked at before the probe existed — so without it the
    selection is decided by tie-break among backends that are not equivalent.
    """

    control = ControlPlaneService(async_services.context)
    stub = control.create_stub(f"pod-readiness-{uuid4().hex[:8]}", kind=StubKind.Pod)
    with async_services.context.database.session() as session:
        workspace_id = async_services.context.default_workspace_id(session)
        repository = ContainerRepository(session)
        for container_id in (SERVING_CONTAINER, STARTED_CONTAINER):
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=f"pod-{container_id[-4:]}",
                    image="image",
                    command=["sleep", "300"],
                    workspace_id=workspace_id,
                    stub_id=stub.id,
                    status=ContainerStatus.Running,
                )
            )

    service = replace(
        async_services.pod_service,
        async_scheduler_containers=_RunningContainers(
            workspace_id=workspace_id,
            stub_id=stub.id,
        ),
        container_readiness_probe=_OnlyOneIsServing(serving_container_id=SERVING_CONTAINER),
    )

    session = await service.prepare_pod_proxy(
        stub_id=stub.id,
        port=PORT,
        path="/",
        query_params={},
        protocol=PodProxyProtocol.Http,
    )
    assert session.target.container_id == SERVING_CONTAINER
    await service.finish_pod_proxy(session)
