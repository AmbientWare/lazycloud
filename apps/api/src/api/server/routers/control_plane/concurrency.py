from __future__ import annotations

from control.service import ControlServices
from fastapi import APIRouter, Depends, Response, status
from shared.http.concurrency import (
    ConcurrencyAcquireResponse,
    ConcurrencyLimitListResponse,
    ConcurrencyLimitResponse,
    ConcurrencyLimitSetRequest,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import control_plane_service

router = APIRouter()


@router.get(
    "/api/v1/concurrency-limits",
    response_model=ConcurrencyLimitListResponse,
    operation_id="list_concurrency_limits",
)
def list_concurrency_limits(
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyLimitListResponse:
    return ConcurrencyLimitListResponse(
        limits=[
            ConcurrencyLimitResponse.model_validate(record)
            for record in service.concurrency.list_concurrency_limits(workspace=workspace_id)
        ]
    )


@router.post(
    "/api/v1/concurrency-limits",
    response_model=ConcurrencyLimitResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="set_concurrency_limit",
)
def set_concurrency_limit(
    request: ConcurrencyLimitSetRequest,
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return ConcurrencyLimitResponse.model_validate(
        service.concurrency.upsert_concurrency_limit(
            request.name,
            workspace=workspace_id,
            limit=request.limit,
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            metadata=request.metadata,
        )
    )


@router.get(
    "/api/v1/concurrency-limits/current",
    response_model=ConcurrencyLimitResponse,
    operation_id="get_current_concurrency_limit",
)
def current_concurrency_limit(
    workspace_id: read_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return ConcurrencyLimitResponse.model_validate(
        service.concurrency.current_concurrency_limit(workspace=workspace_id)
    )


@router.delete(
    "/api/v1/concurrency-limits/current",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_current_concurrency_limit",
)
def delete_current_concurrency_limit(
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> None:
    service.concurrency.delete_current_concurrency_limit(workspace=workspace_id)


@router.post(
    "/api/v1/concurrency-limits/revert",
    response_model=ConcurrencyLimitResponse,
    operation_id="revert_concurrency_limit",
)
def revert_concurrency_limit(
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return ConcurrencyLimitResponse.model_validate(
        service.concurrency.revert_concurrency_limit(workspace=workspace_id)
    )


@router.post(
    "/api/v1/concurrency-limits/{limit_id_or_name}/acquire",
    response_model=ConcurrencyAcquireResponse,
    operation_id="acquire_concurrency_limit",
)
def acquire_concurrency_limit(
    limit_id_or_name: str,
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyAcquireResponse:
    return ConcurrencyAcquireResponse.model_validate(
        service.concurrency.acquire_concurrency(
            limit_id_or_name,
            workspace=workspace_id,
        )
    )


@router.post(
    "/api/v1/concurrency-limits/{limit_id_or_name}/release",
    response_model=ConcurrencyAcquireResponse,
    operation_id="release_concurrency_limit",
)
def release_concurrency_limit(
    limit_id_or_name: str,
    workspace_id: write_workspace,
    service: ControlServices = Depends(control_plane_service),
) -> ConcurrencyAcquireResponse:
    return ConcurrencyAcquireResponse.model_validate(
        service.concurrency.release_concurrency(
            limit_id_or_name,
            workspace=workspace_id,
        )
    )
