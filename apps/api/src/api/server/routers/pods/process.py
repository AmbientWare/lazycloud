from __future__ import annotations

from execution.pods.service import PodControlService
from fastapi import APIRouter, Depends
from shared.http.pods import (
    CreatePodRequest,
    CreatePodResponse,
    PodSandboxExecRequest,
    PodSandboxExecResponse,
    PodSandboxKillRequest,
    PodSandboxKillResponse,
    PodSandboxListProcessesResponse,
    PodSandboxStatusResponse,
    PodSandboxStderrResponse,
    PodSandboxStdoutResponse,
)

from api.server.auth import write_workspace
from api.server.routers.pods.common import read_container, write_container
from api.server.service_dependencies import pod_service

router = APIRouter(prefix="/api/v1/pods", tags=["pods"])


@router.post("", response_model=CreatePodResponse)
def create_pod(
    request: CreatePodRequest,
    workspace_id: write_workspace,
    service: PodControlService = Depends(pod_service),
) -> CreatePodResponse:
    return service.create_pod(request, authorized_workspace_id=workspace_id)


@router.post("/{container_id}/exec", response_model=PodSandboxExecResponse)
def sandbox_exec(
    container_id: str,
    request: PodSandboxExecRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxExecResponse:
    return service.sandbox_exec(container_id, request)


@router.get("/{container_id}/status", response_model=PodSandboxStatusResponse)
def sandbox_status(
    container_id: str,
    pid: int,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxStatusResponse:
    return service.sandbox_status(container_id, pid)


@router.get("/{container_id}/stdout", response_model=PodSandboxStdoutResponse)
def sandbox_stdout(
    container_id: str,
    pid: int,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxStdoutResponse:
    return service.sandbox_stdout(container_id, pid)


@router.get("/{container_id}/stderr", response_model=PodSandboxStderrResponse)
def sandbox_stderr(
    container_id: str,
    pid: int,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxStderrResponse:
    return service.sandbox_stderr(container_id, pid)


@router.post("/{container_id}/kill", response_model=PodSandboxKillResponse)
def sandbox_kill(
    container_id: str,
    request: PodSandboxKillRequest,
    _auth: write_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxKillResponse:
    return service.sandbox_kill(container_id, request)


@router.get("/{container_id}/processes", response_model=PodSandboxListProcessesResponse)
def sandbox_list_processes(
    container_id: str,
    _auth: read_container,
    service: PodControlService = Depends(pod_service),
) -> PodSandboxListProcessesResponse:
    return service.sandbox_list_processes(container_id)
