from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
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
from api.server.sse import sse_event

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


@router.get("/agents/stream/events", response_class=StreamingResponse)
def stream_agent_events(
    agent_token: str = Query(...),
    poll_interval_seconds: float = Query(5.0, ge=0.1, le=300),
    max_events: int = Query(0, ge=0),
    service: GatewayControlService = Depends(gateway_service),
) -> StreamingResponse:
    return StreamingResponse(
        _agent_stream_events(
            service,
            agent_token=agent_token,
            poll_interval_seconds=poll_interval_seconds,
            max_events=max_events,
        ),
        media_type="text/event-stream",
    )


@router.post("/agents/telemetry", response_model=AgentTelemetryResponse)
def stream_agent_telemetry(
    request: AgentTelemetryRequest,
    service: GatewayControlService = Depends(gateway_service),
) -> AgentTelemetryResponse:
    return service.stream_agent_telemetry(request)


@router.post("/agents/telemetry/stream", response_model=AgentTelemetryResponse)
async def stream_agent_telemetry_batches(
    request: Request,
    service: GatewayControlService = Depends(gateway_service),
) -> AgentTelemetryResponse:
    return await _consume_telemetry_lines(service, request)


async def _agent_stream_events(
    service: GatewayControlService,
    *,
    agent_token: str,
    poll_interval_seconds: float,
    max_events: int,
) -> AsyncIterator[str]:
    yield ": connected\n\n"
    sent = 0
    while max_events == 0 or sent < max_events:
        response = service.stream_agent(StreamAgentRequest(agent_token=agent_token))
        event = "agent.snapshot" if response.ok else "agent.error"
        yield sse_event(event, agent_token, response)
        sent += 1
        if not response.ok:
            return
        await asyncio.sleep(poll_interval_seconds)


async def _consume_telemetry_lines(
    service: GatewayControlService,
    request: Request,
) -> AgentTelemetryResponse:
    buffer = b""
    agent_token = ""
    async for chunk in request.stream():
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            response = _record_telemetry_line(service, line, agent_token)
            if response[0] is not None:
                agent_token = response[0]
            if not response[1].ok:
                return response[1]
    response = _record_telemetry_line(service, buffer, agent_token)
    if response[0] is not None:
        agent_token = response[0]
    return response[1]


def _record_telemetry_line(
    service: GatewayControlService,
    line: bytes,
    expected_agent_token: str,
) -> tuple[str | None, AgentTelemetryResponse]:
    stripped = line.strip()
    if not stripped:
        return None, AgentTelemetryResponse(ok=True)
    try:
        telemetry = AgentTelemetryRequest.model_validate_json(stripped)
    except ValueError as exc:
        return None, AgentTelemetryResponse(ok=False, err_msg=str(exc))
    if expected_agent_token and telemetry.agent_token != expected_agent_token:
        return None, AgentTelemetryResponse(
            ok=False,
            err_msg="agent token changed on telemetry stream",
        )
    return telemetry.agent_token, service.stream_agent_telemetry(telemetry)
