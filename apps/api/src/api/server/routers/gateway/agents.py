from __future__ import annotations

from fastapi import APIRouter, Depends
from gateway.http import (
    AgentTelemetryRequest,
    AgentTelemetryResponse,
    JoinAgentRequest,
    JoinAgentResponse,
    LeaveAgentRequest,
    LeaveAgentResponse,
    ListAgentRoutesRequest,
    ListAgentRoutesResponse,
    RegisterAgentPrivateNetworkRequest,
    RegisterAgentPrivateNetworkResponse,
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from gateway.service import GatewayControlService
from shared.http.gateway import (
    AgentCapacityInterruptionRequest,
    AgentCapacityInterruptionResponse,
)
from shared.http.provider_nodes import (
    ProviderNodeBootstrapFailureRequest,
    ProviderNodeBootstrapFailureResponse,
    ProviderNodeBootstrapPhaseRequest,
    ProviderNodeEnrollmentRequest,
)

from api.server.service_dependencies import gateway_service, provider_node_enrollment_service

router = APIRouter(prefix="/gateway", tags=["gateway"])


@router.post(
    "/provider-nodes/enroll",
    response_model=JoinAgentResponse,
    operation_id="enroll_provider_node",
)
def enroll_provider_node(
    request: ProviderNodeEnrollmentRequest,
    service: ProviderNodeEnrollmentService = Depends(provider_node_enrollment_service),
) -> JoinAgentResponse:
    return service.enroll(request)


@router.post(
    "/provider-nodes/bootstrap-failure",
    response_model=ProviderNodeBootstrapFailureResponse,
    operation_id="record_provider_node_bootstrap_failure",
)
def record_provider_node_bootstrap_failure(
    request: ProviderNodeBootstrapFailureRequest,
    service: ProviderNodeEnrollmentService = Depends(provider_node_enrollment_service),
) -> ProviderNodeBootstrapFailureResponse:
    return service.report_failure(request)


@router.post(
    "/provider-nodes/bootstrap-phase",
    response_model=ProviderNodeBootstrapFailureResponse,
    operation_id="record_provider_node_bootstrap_phase",
)
def record_provider_node_bootstrap_phase(
    request: ProviderNodeBootstrapPhaseRequest,
    service: ProviderNodeEnrollmentService = Depends(provider_node_enrollment_service),
) -> ProviderNodeBootstrapFailureResponse:
    return service.record_phase(request)


@router.post("/agents/join", response_model=JoinAgentResponse)
def join_agent(
    request: JoinAgentRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> JoinAgentResponse:
    return service.join_agent(request)


@router.post(
    "/agents/leave",
    response_model=LeaveAgentResponse,
    operation_id="leave_agent",
)
def leave_agent(
    request: LeaveAgentRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> LeaveAgentResponse:
    return service.leave_agent(request)


@router.post(
    "/agents/private-network",
    response_model=RegisterAgentPrivateNetworkResponse,
    operation_id="register_agent_private_network",
)
def register_agent_private_network(
    request: RegisterAgentPrivateNetworkRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> RegisterAgentPrivateNetworkResponse:
    return service.register_agent_private_network(request)


@router.post("/agents/routes", response_model=ListAgentRoutesResponse)
def list_agent_routes(
    request: ListAgentRoutesRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> ListAgentRoutesResponse:
    return service.list_agent_routes(request)


@router.post("/agents/routes/status", response_model=UpdateAgentRouteStatusResponse)
def update_agent_route_status(
    request: UpdateAgentRouteStatusRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> UpdateAgentRouteStatusResponse:
    return service.update_agent_route_status(request)


@router.post("/agents/stream", response_model=StreamAgentResponse)
def stream_agent(
    request: StreamAgentRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> StreamAgentResponse:
    return service.stream_agent(request)


@router.post(
    "/agents/capacity-interruption",
    response_model=AgentCapacityInterruptionResponse,
    operation_id="record_agent_capacity_interruption",
)
def record_agent_capacity_interruption(
    request: AgentCapacityInterruptionRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> AgentCapacityInterruptionResponse:
    return service.record_agent_capacity_interruption(request)


@router.post("/agents/telemetry", response_model=AgentTelemetryResponse)
def stream_agent_telemetry(
    request: AgentTelemetryRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> AgentTelemetryResponse:
    return service.stream_agent_telemetry(request)
