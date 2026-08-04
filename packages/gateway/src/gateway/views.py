from __future__ import annotations

from enum import StrEnum

from compute.agent_control import (
    WorkerRecord,
    WorkerStatus,
)
from compute.projection import PoolConfig, PrivatePoolState
from compute.state import (
    ComputeAgentRouteState,
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputePoolState,
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
    MachineReadinessPhase,
)
from shared.compute_fleet import Machine, Pool, ResourceStatus
from shared.errors import NotFoundError
from shared.http.compute import PoolMachineMetricsResponse, PoolMachineResponse
from shared.routing import (
    AgentBackendRoute,
    BackendRouteKind,
    BackendRouteProtocol,
    BackendRouteState,
    BackendRouteTransport,
)
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus
from shared.tasks import Task

from compute import agent_control, projection
from gateway.http import (
    AgentBootstrapConfig,
    AgentRoute,
    AgentWorkerSlot,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def pool_config_from_pool(pool: Pool) -> PoolConfig:
    provider = pool.provider or "local"
    return PoolConfig(
        name=pool.name,
        providers=[provider],
        gpu=[pool.labels["gpu"]] if pool.labels.get("gpu") else [],
        nodes=pool.max_workers,
        ttl=pool.labels.get("ttl", ""),
        max_spend=float(pool.labels.get("max_spend", "0") or 0),
        selector=pool.labels.get("selector", pool.name),
        mode=pool.labels.get("mode", "private"),
        transport=pool.labels.get("transport", ""),
        fallback=pool.labels.get("fallback", ""),
        priority=int(pool.labels.get("priority", "0") or 0),
        offer_id=pool.labels.get("offer_id", ""),
    )


def private_pool_from_compute_state(state: ComputePoolState) -> PrivatePoolState:
    raw_config = state.metadata.get("config")
    config = (
        projection.PoolConfig.model_validate(raw_config)
        if isinstance(raw_config, dict)
        else projection.PoolConfig(name=state.name)
    )
    return PrivatePoolState(
        workspace_id=state.workspace_id,
        name=state.name,
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
) -> PoolMachineResponse:
    memory = _memory_mb(machine.memory)
    gpu = machine.gpu or machine.labels.get("gpu", "")
    gpu_count = int(machine.labels.get("gpu_count", "1") or 1) if gpu else 0
    readiness_phase = _machine_readiness_phase(machine, agent_state)
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
    return PoolMachineResponse(
        id=machine.id,
        cpu=int((machine.cpu or 0) * 1000),
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        status=machine.status.value,
        pool_name=machine.pool,
        provider_name=machine.provider,
        readiness_phase=readiness_phase,
        readiness_message=(
            agent_state.capacity_reason
            if agent_state is not None and agent_state.capacity_reason
            else _machine_readiness_message(readiness_phase, preflight_checks)
        ),
        schedulable=(
            readiness_phase is MachineReadinessPhase.Ready
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
            agent_machine_last_seen(agent_telemetry_state(agent_state))
            if agent_state is not None
            else machine.updated_at
        ),
        created_at=machine.created_at,
        agent_version=agent_state.agent_version if agent_state is not None else "",
        machine_metrics=PoolMachineMetricsResponse(
            total_cpu_available=int((machine.cpu or 0) * 1000),
            total_memory_available=memory,
            free_gpu_count=gpu_count,
            memory_total_mb=memory,
        ),
    )


def _machine_readiness_phase(
    machine: Machine,
    agent_state: ComputeAgentTokenState | None,
) -> MachineReadinessPhase:
    if agent_state is not None:
        if not agent_state.preflight_passed:
            return MachineReadinessPhase.Blocked
        if not agent_state.heartbeat_confirmed:
            return MachineReadinessPhase.Joining
        if agent_machine_connected(agent_telemetry_state(agent_state)):
            return MachineReadinessPhase.Ready
        if agent_state.last_heartbeat_at is None:
            return MachineReadinessPhase.Joining
        return MachineReadinessPhase.Offline
    return {
        ResourceStatus.Running: MachineReadinessPhase.Ready,
        ResourceStatus.Failed: MachineReadinessPhase.Blocked,
        ResourceStatus.Stopped: MachineReadinessPhase.Offline,
        ResourceStatus.Deleted: MachineReadinessPhase.Revoked,
        ResourceStatus.Created: MachineReadinessPhase.Joining,
    }[machine.status]


def _machine_readiness_message(
    phase: MachineReadinessPhase,
    preflight: list[ComputePreflightCheck],
) -> str:
    if phase is MachineReadinessPhase.Ready:
        return "Ready for workloads"
    if phase is MachineReadinessPhase.Blocked:
        failed = [check.message for check in preflight if check.required and not check.ok]
        return "; ".join(item for item in failed if item) or "Host preflight failed"
    if phase is MachineReadinessPhase.Offline:
        return "Agent heartbeat is stale"
    if phase is MachineReadinessPhase.Revoked:
        return "Machine access was revoked"
    return "Waiting for the agent to connect"


def agent_bootstrap_view(config: agent_control.AgentBootstrapConfig) -> AgentBootstrapConfig:
    return AgentBootstrapConfig(
        gateway_public_http_url=config.gateway_public_http_url,
        gateway_runtime_http_url=config.gateway_runtime_http_url,
        gateway_grpc_host=config.gateway_grpc_host,
        gateway_grpc_port=config.gateway_grpc_port,
        gateway_grpc_tls=config.gateway_grpc_tls,
        workspace_id=config.workspace_id,
        pool_name=config.pool_name,
        transport=config.transport,
        executor=config.executor,
        fallback=config.fallback,
        image_registry_store=config.image_registry_store,
        image_clip_version=config.image_clip_version,
        image_local_cache_enabled=config.image_local_cache_enabled,
    )


def agent_backend_route(route: ComputeAgentRouteState) -> AgentBackendRoute:
    return AgentBackendRoute(
        route_id=route.route_id,
        workspace_id=route.workspace_id,
        pool_name=route.pool_name,
        machine_id=route.machine_id,
        worker_id=route.worker_id,
        container_id=route.container_id,
        kind=route.kind,
        port=route.port,
        protocol=route.protocol,
        transport=route.transport,
        local_target=route.local_target,
        proxy_target=route.proxy_target,
        state=route.state,
        error=route.error,
        updated_at=route.updated_at,
    )


def agent_route_state(route: AgentBackendRoute) -> ComputeAgentRouteState:
    return ComputeAgentRouteState(
        route_id=route.route_id,
        workspace_id=route.workspace_id,
        pool_name=route.pool_name,
        machine_id=route.machine_id,
        worker_id=route.worker_id,
        container_id=route.container_id,
        kind=_enum_value(route.kind),
        port=route.port,
        protocol=_enum_value(route.protocol),
        transport=_enum_value(route.transport),
        local_target=route.local_target,
        proxy_target=route.proxy_target,
        state=_enum_value(route.state),
        error=route.error,
        updated_at=route.updated_at,
    )


def agent_route_view(
    route: ComputeAgentRouteState | AgentBackendRoute,
    *,
    proxy_auth_token: str = "",
) -> AgentRoute:
    return AgentRoute(
        route_id=route.route_id,
        workspace_id=route.workspace_id,
        pool_name=route.pool_name,
        machine_id=route.machine_id,
        worker_id=route.worker_id,
        container_id=route.container_id,
        kind=_backend_route_kind(route.kind),
        port=route.port,
        protocol=_backend_route_protocol(route.protocol),
        transport=_backend_route_transport(route.transport),
        local_target=route.local_target,
        proxy_target=route.proxy_target,
        state=_backend_route_state(route.state),
        error=route.error,
        updated_at=route.updated_at,
        proxy_auth_token=proxy_auth_token,
    )


def agent_worker_slot_view(slot: ComputeAgentWorkerSlotState) -> AgentWorkerSlot:
    worker_token = slot.metadata.get("worker_token")
    return AgentWorkerSlot(
        worker_id=slot.worker_id,
        worker_token=str(worker_token) if worker_token is not None else "",
        pool_name=slot.pool_name,
        capacity_owner_id=slot.capacity_owner_id,
        machine_id=slot.machine_id,
        cpu=slot.cpu,
        memory=slot.memory,
        gpu=slot.gpu,
        gpu_count=slot.gpu_count,
        gpu_assignment=slot.gpu_assignment,
        network_prefix=slot.network_prefix,
        worker_image=slot.worker_image,
    )


def agent_worker_record(worker: SchedulerWorkerRecord) -> WorkerRecord:
    return WorkerRecord(
        id=worker.worker_id,
        machine_id=worker.machine_id,
        pool_name=worker.pool_name,
        capacity_owner_id=worker.capacity_owner_id,
        status=_agent_worker_status(worker.status),
        total_cpu=worker.total_cpu_millicores,
        total_memory=worker.total_memory_mib,
        gpu=worker.gpu_type,
        total_gpu_count=worker.total_gpu_count,
    )


def agent_pool_transport(state: ComputeAgentTokenState) -> str:
    value = state.metadata.get("pool_transport")
    return str(value) if value is not None else ""


def _agent_worker_status(status: SchedulerWorkerStatus) -> WorkerStatus:
    if status is SchedulerWorkerStatus.Available:
        return WorkerStatus.Available
    if status is SchedulerWorkerStatus.Pending:
        return WorkerStatus.Pending
    return WorkerStatus.Disabled


def _enum_value(value: StrEnum | str) -> str:
    return value.value if isinstance(value, StrEnum) else value


def _backend_route_kind(value: BackendRouteKind | str) -> BackendRouteKind:
    if isinstance(value, BackendRouteKind):
        return value
    return BackendRouteKind(value)


def _backend_route_protocol(value: BackendRouteProtocol | str) -> BackendRouteProtocol:
    if isinstance(value, BackendRouteProtocol):
        return value
    return BackendRouteProtocol(value)


def _backend_route_transport(value: BackendRouteTransport | str) -> BackendRouteTransport:
    if isinstance(value, BackendRouteTransport):
        return value
    return BackendRouteTransport(value)


def _backend_route_state(value: BackendRouteState | str) -> BackendRouteState:
    if isinstance(value, BackendRouteState):
        return value
    return BackendRouteState(value)


def stub_for_task(control_plane: ControlPlaneService, task: Task) -> StubRecord | None:
    if task.stub_id:
        try:
            return control_plane.get_stub(task.stub_id)
        except NotFoundError:
            pass
    if task.deployment_id is None:
        return None
    return stub_for_deployment(control_plane, task.deployment_id, workspace=None)


def stub_for_deployment(
    control_plane: ControlPlaneService,
    deployment_id: str,
    *,
    workspace: str | None,
) -> StubRecord | None:
    return next(
        (
            item
            for item in control_plane.list_stubs(workspace=workspace)
            if item.deployment_id == deployment_id
        ),
        None,
    )


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
