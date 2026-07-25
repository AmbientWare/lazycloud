from __future__ import annotations

from collections.abc import Sequence

from control.models import ConcurrencyAcquireResult
from control.service import ControlPlaneService
from fastapi import APIRouter, Depends, Response, status
from shared.http.concurrency import (
    ConcurrencyAcquireResponse,
    ConcurrencyLimitListResponse,
    ConcurrencyLimitResponse,
    ConcurrencyLimitSetRequest,
)
from shared.identity import ConcurrencyLimitRecord

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import control_plane_service

router = APIRouter()


def _limit_response(record: ConcurrencyLimitRecord) -> ConcurrencyLimitResponse:
    payload = record.model_dump(mode="json")
    payload["available"] = record.available
    payload["saturated"] = record.saturated
    return ConcurrencyLimitResponse.model_validate(payload)


def _limit_list_response(records: Sequence[ConcurrencyLimitRecord]) -> ConcurrencyLimitListResponse:
    return ConcurrencyLimitListResponse(limits=[_limit_response(item) for item in records])


def _acquire_response(result: ConcurrencyAcquireResult) -> ConcurrencyAcquireResponse:
    return ConcurrencyAcquireResponse(
        status=result.status.value,
        acquired=result.acquired,
        record=_limit_response(result.record),
        available_before=result.available_before,
        available_after=result.available_after,
        reason=result.reason,
    )


@router.get(
    "/api/v1/concurrency-limits",
    response_model=ConcurrencyLimitListResponse,
    operation_id="list_concurrency_limits",
)
def list_concurrency_limits(
    workspace_id: read_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyLimitListResponse:
    return _limit_list_response(service.list_concurrency_limits(workspace=workspace_id))


@router.post(
    "/api/v1/concurrency-limits",
    response_model=ConcurrencyLimitResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="set_concurrency_limit",
)
def set_concurrency_limit(
    request: ConcurrencyLimitSetRequest,
    workspace_id: write_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return _limit_response(
        service.upsert_concurrency_limit(
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
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return _limit_response(service.current_concurrency_limit(workspace=workspace_id))


@router.delete(
    "/api/v1/concurrency-limits/current",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_current_concurrency_limit",
)
def delete_current_concurrency_limit(
    workspace_id: write_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> None:
    service.delete_current_concurrency_limit(workspace=workspace_id)


@router.post(
    "/api/v1/concurrency-limits/revert",
    response_model=ConcurrencyLimitResponse,
    operation_id="revert_concurrency_limit",
)
def revert_concurrency_limit(
    workspace_id: write_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyLimitResponse:
    return _limit_response(service.revert_concurrency_limit(workspace=workspace_id))


@router.post(
    "/api/v1/concurrency-limits/{limit_id_or_name}/acquire",
    response_model=ConcurrencyAcquireResponse,
    operation_id="acquire_concurrency_limit",
)
def acquire_concurrency_limit(
    limit_id_or_name: str,
    workspace_id: write_workspace,
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyAcquireResponse:
    return _acquire_response(
        service.acquire_concurrency(
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
    service: ControlPlaneService = Depends(control_plane_service),
) -> ConcurrencyAcquireResponse:
    return _acquire_response(
        service.release_concurrency(
            limit_id_or_name,
            workspace=workspace_id,
        )
    )
