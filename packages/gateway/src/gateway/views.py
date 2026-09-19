from __future__ import annotations

from compute.projection import ComputeUnitMode, PoolConfig, PrivateUnitState
from compute.state import (
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputeUnitState,
)
from compute.telemetry import (
    agent_machine_connected,
    agent_machine_last_seen,
    agent_telemetry_state,
)
from control.service import ControlPlaneService, StubRecord
from pydantic import JsonValue, TypeAdapter
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputePreflightCheck,
)
from shared.compute_fleet import Machine, MachineLifecycle
from shared.compute_policy import ComputeUnitRecord
from shared.errors import NotFoundError
from shared.http.compute import UnitMachineMetricsResponse, UnitMachineResponse
from shared.routing import (
    AgentBackendRoute,
)
from shared.tasks import Task

from compute import projection
from gateway.http import (
    AgentRoute,
    AgentWorkerSlot,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def pool_config_from_unit(pool: ComputeUnitRecord) -> PoolConfig:
    """Project a provisioning unit into the config its agents are given."""
    return PoolConfig(
        name=pool.name,
        providers=[pool.provider],
        gpu=[pool.worker_gpu_type] if pool.worker_gpu_type else [],
        nodes=pool.max_machines,
        selector=pool.selector or pool.name,
        mode=ComputeUnitMode.Private,
        fallback=pool.fallback,
        priority=pool.priority,
        offer_id=pool.offer_id,
    )


def private_pool_from_compute_state(state: ComputeUnitState) -> PrivateUnitState:
    raw_config = state.metadata.get("config")
    config = (
        projection.PoolConfig.model_validate(raw_config)
        if isinstance(raw_config, dict)
        else projection.PoolConfig(name=state.name)
    )
    return PrivateUnitState(
        platform_fleet=state.platform_fleet,
        workspace_id=state.workspace_id,
        name=state.name,
        placement=state.placement,
        capacity_owner_id=state.capacity_owner_id,
        selector=config.selector or state.name,
        config=config,
        status=state.status.value,
        created_by_token_id=str(state.metadata.get("created_by_token_id") or ""),
        reserved_nodes=state.max_machines,
    )


def machine_view(
    machine: Machine,
    agent_state: ComputeAgentTokenState | None = None,
    *,
    tunnel_connected: bool,
    workspace_names: list[str] | None = None,
) -> UnitMachineResponse:
    """Project one machine row and its live agent state onto the wire.

    The lifecycle comes from the row; the view adds what only the running
    control plane knows, which is whether the agent's tunnel is open and what
    its host checks said.
    """
    memory = _memory_mb(machine.memory)
    gpu = machine.gpu or ""
    gpu_count = (machine.gpu_count or 1) if gpu else 0
    telemetry = agent_telemetry_state(agent_state) if agent_state is not None else None
    connected = (
        telemetry is not None
        and agent_state is not None
        and agent_state.heartbeat_confirmed
        and agent_machine_connected(telemetry.model_copy(update={"schedulable": True}))
        and tunnel_connected
    )
    preflight_checks = [
        ComputePreflightCheck(
            name=check.name,
            ok=check.ok,
            message=check.message,
            severity=check.severity,
            remediation=(
                check.remediation or (check.message if check.required and not check.ok else "")
            ),
        )
        for check in (agent_state.preflight if agent_state is not None else [])
    ]
    remediation = [check.remediation for check in preflight_checks if check.remediation]
    lifecycle_message = machine.lifecycle_message
    if (
        machine.lifecycle is MachineLifecycle.Joining
        and agent_state is not None
        and agent_state.heartbeat_confirmed
        and not tunnel_connected
    ):
        lifecycle_message = "Waiting for the agent tunnel to connect"
    return UnitMachineResponse(
        id=machine.id,
        cpu=int((machine.cpu or 0) * 1000),
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        name=machine.name,
        workspaces=list(workspace_names or []),
        placement=machine.placement,
        lifecycle=machine.lifecycle,
        lifecycle_message=lifecycle_message,
        lifecycle_failure=machine.lifecycle_failure,
        lifecycle_at=machine.lifecycle_at,
        connected=connected,
        schedulable=(
            machine.lifecycle is MachineLifecycle.Ready
            and connected
            and (agent_state is None or agent_state.schedulable)
        ),
        capacity_state=(
            agent_state.capacity_state if agent_state is not None else AgentCapacityState.Available
        ),
        capacity_reason=agent_state.capacity_reason if agent_state is not None else "",
        capacity_observed_at=(
            agent_state.capacity_observed_at if agent_state is not None else None
        ),
        capacity_notice_at=agent_state.capacity_notice_at if agent_state is not None else None,
        preflight_checks=preflight_checks,
        remediation=list(dict.fromkeys(remediation)),
        last_seen_at=(
            agent_machine_last_seen(telemetry) if telemetry is not None else machine.updated_at
        ),
        created_at=machine.created_at,
        agent_version=agent_state.agent_version if agent_state is not None else "",
        machine_metrics=UnitMachineMetricsResponse(
            total_cpu_available=int((machine.cpu or 0) * 1000),
            total_memory_available=memory,
            free_gpu_count=gpu_count,
            memory_total_mb=memory,
        ),
    )


def agent_route_view(
    route: AgentBackendRoute,
) -> AgentRoute:
    """Project a backend route onto the agent wire contract.

    Written out field by field rather than copied wholesale: the wire contract
    is public, so a field added to the domain route must be an explicit
    decision to publish it.
    """
    return AgentRoute(
        route_id=route.route_id,
        workspace_id=route.workspace_id,
        placement=route.placement,
        machine_id=route.machine_id,
        worker_id=route.worker_id,
        container_id=route.container_id,
        kind=route.kind,
        port=route.port,
        protocol=route.protocol,
        local_target=route.local_target,
        state=route.state,
        error=route.error,
        updated_at=route.updated_at,
    )


def agent_worker_slot_view(slot: ComputeAgentWorkerSlotState) -> AgentWorkerSlot:
    worker_token = slot.metadata.get("worker_token")
    return AgentWorkerSlot(
        worker_id=slot.worker_id,
        worker_token=str(worker_token) if worker_token is not None else "",
        placement=slot.placement,
        capacity_owner_id=slot.capacity_owner_id,
        billing_owner=slot.billing_owner,
        machine_id=slot.machine_id,
        cpu=slot.cpu,
        memory=slot.memory,
        gpu=slot.gpu,
        gpu_count=slot.gpu_count,
        gpu_assignment=slot.gpu_assignment,
        network_prefix=slot.network_prefix,
        worker_image=slot.worker_image,
        status=slot.status,
    )


def stub_for_task(control_plane: ControlPlaneService, task: Task) -> StubRecord | None:
    if task.stub_id:
        try:
            return control_plane.get_stub(task.stub_id, workspace=task.workspace_id)
        except NotFoundError:
            pass
    if task.deployment_id is None:
        return None
    return control_plane.get_deployment_stub(task.deployment_id, workspace=task.workspace_id)


def _memory_mb(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return 0
    amount = int(digits)
    lowered = value.lower()
    if "g" in lowered:
        return amount * 1024
    return amount
