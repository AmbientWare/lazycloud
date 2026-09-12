from __future__ import annotations

from pydantic import Field, field_validator
from shared.compute_policy import MachinePool
from shared.contracts import ContractModel
from shared.routing import (
    AgentBackendRoute,
    BackendRouteKind,
    BackendRouteProtocol,
    BackendRouteState,
)


class WorkerRouteRegistrationPlan(ContractModel):
    container_id: str
    ok: bool
    primary_port: int = 0
    primary_target: str = ""
    address_map: dict[int, str] = Field(default_factory=dict)
    routes: list[AgentBackendRoute] = Field(default_factory=list)
    error_message: str = ""


class WorkerRouteContext(ContractModel):
    workspace_id: str
    pool: MachinePool
    machine_id: str
    worker_id: str
    container_id: str


class WorkerPortBinding(ContractModel):
    container_port: int

    @field_validator("container_port")
    @classmethod
    def container_port_must_be_valid(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            msg = "container port must be between 1 and 65535"
            raise ValueError(msg)
        return value


def backend_route_id(
    *,
    machine_id: str,
    worker_id: str,
    container_id: str,
    kind: BackendRouteKind | str,
    port: int,
) -> str:
    return ":".join([machine_id, worker_id, container_id, str(kind), str(port)])


def build_agent_backend_route(
    context: WorkerRouteContext,
    *,
    kind: BackendRouteKind = BackendRouteKind.Container,
    port: int,
    local_target: str,
) -> AgentBackendRoute:
    return AgentBackendRoute(
        route_id=backend_route_id(
            machine_id=context.machine_id,
            worker_id=context.worker_id,
            container_id=context.container_id,
            kind=kind,
            port=port,
        ),
        workspace_id=context.workspace_id,
        pool=MachinePool(context.pool),
        machine_id=context.machine_id,
        worker_id=context.worker_id,
        container_id=context.container_id,
        kind=kind,
        port=port,
        protocol=BackendRouteProtocol.Tcp,
        local_target=local_target,
        state=BackendRouteState.Opening,
    )


def plan_container_route_registration(
    context: WorkerRouteContext,
    *,
    bindings: list[WorkerPortBinding],
    address_map: dict[int, str],
) -> WorkerRouteRegistrationPlan:
    if not bindings:
        return WorkerRouteRegistrationPlan(
            container_id=context.container_id,
            ok=True,
            address_map=dict(address_map),
        )
    primary_port = bindings[0].container_port
    primary_target = address_map.get(primary_port, "")
    if not primary_target:
        error_message = f"container {context.container_id} has no address for port {primary_port}"
        return WorkerRouteRegistrationPlan(
            container_id=context.container_id,
            ok=False,
            primary_port=primary_port,
            address_map=dict(address_map),
            error_message=error_message,
        )
    routes: list[AgentBackendRoute] = []
    for binding in bindings:
        local_target = address_map.get(binding.container_port, "")
        route = build_agent_backend_route(
            context,
            port=binding.container_port,
            local_target=local_target,
        )
        routes.append(route)
    return WorkerRouteRegistrationPlan(
        container_id=context.container_id,
        ok=True,
        primary_port=primary_port,
        primary_target=primary_target,
        address_map=dict(address_map),
        routes=routes,
    )
