from __future__ import annotations

from execution.signals.redis import RedisSignalService
from fastapi import APIRouter, Depends
from pydantic import Field
from shared.http.base import HttpModel
from shared.signals import (
    DEFAULT_SIGNAL_SET_TTL_SECONDS,
    SignalClearRequest,
    SignalClearResponse,
    SignalMonitorRequest,
    SignalMonitorResponse,
    SignalSetRequest,
    SignalSetResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import signal_service


class SignalSetBody(HttpModel):
    ttl_seconds: int = Field(default=DEFAULT_SIGNAL_SET_TTL_SECONDS)


router = APIRouter(prefix="/api/v1/signals", tags=["signal"])


@router.post("/{name:path}/set", response_model=SignalSetResponse)
def set_signal(
    name: str,
    request: SignalSetBody,
    workspace_id: write_workspace,
    service: RedisSignalService = Depends(signal_service),
) -> SignalSetResponse:
    return service.signal_set(
        SignalSetRequest(
            workspace_name=workspace_id,
            name=name,
            ttl_seconds=request.ttl_seconds,
        )
    )


@router.post("/{name:path}/clear", response_model=SignalClearResponse)
def clear_signal(
    name: str,
    workspace_id: write_workspace,
    service: RedisSignalService = Depends(signal_service),
) -> SignalClearResponse:
    return service.signal_clear(SignalClearRequest(workspace_name=workspace_id, name=name))


@router.get("/{name:path}/monitor", response_model=SignalMonitorResponse)
def monitor_signal_once(
    name: str,
    workspace_id: read_workspace,
    service: RedisSignalService = Depends(signal_service),
) -> SignalMonitorResponse:
    snapshot = service.signal_monitor_once(
        SignalMonitorRequest(workspace_name=workspace_id, name=name)
    )
    return snapshot.response
