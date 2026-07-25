from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from scheduler.workers import SchedulerWorkerAdminService
from shared.http.compute import WorkerDrainResponse, WorkerListResponse, WorkerResponse

from api.server.auth import admin_access
from api.server.dependencies import api_services
from api.server.services import ApiServices

router = APIRouter()


@router.get("/api/v1/workers", response_model=WorkerListResponse, operation_id="list_workers")
def list_workers(
    _auth: admin_access,
    services: ApiServices = Depends(api_services),
) -> WorkerListResponse:
    return WorkerListResponse(
        workers=[
            WorkerResponse.model_validate(item) for item in _worker_admin(services).list_workers()
        ]
    )


@router.delete(
    "/api/v1/workers/{worker_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="delete_worker",
)
def delete_worker(
    worker_id: str,
    _auth: admin_access,
    services: ApiServices = Depends(api_services),
) -> None:
    _worker_admin(services).delete_worker(worker_id)


@router.post(
    "/api/v1/workers/{worker_id}/cordon",
    response_model=WorkerResponse,
    operation_id="cordon_worker",
)
def cordon_worker(
    worker_id: str,
    _auth: admin_access,
    services: ApiServices = Depends(api_services),
) -> WorkerResponse:
    return WorkerResponse.model_validate(_worker_admin(services).cordon_worker(worker_id))


@router.post(
    "/api/v1/workers/{worker_id}/uncordon",
    response_model=WorkerResponse,
    operation_id="uncordon_worker",
)
def uncordon_worker(
    worker_id: str,
    _auth: admin_access,
    services: ApiServices = Depends(api_services),
) -> WorkerResponse:
    return WorkerResponse.model_validate(_worker_admin(services).uncordon_worker(worker_id))


@router.post(
    "/api/v1/workers/{worker_id}/drain",
    response_model=WorkerDrainResponse,
    operation_id="drain_worker",
)
def drain_worker(
    worker_id: str,
    _auth: admin_access,
    services: ApiServices = Depends(api_services),
) -> WorkerDrainResponse:
    result = _worker_admin(services).drain_worker(worker_id)
    return WorkerDrainResponse(
        worker=WorkerResponse.model_validate(result.worker),
        stopped_container_ids=result.stopped_container_ids,
    )


def _worker_admin(services: ApiServices) -> SchedulerWorkerAdminService:
    return services.scheduler_worker_admin_service
