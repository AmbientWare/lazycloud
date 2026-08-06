from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.resource_api import (
    aws_connections,
    compute_containers,
    compute_machines,
    compute_policy,
    compute_units,
    compute_workers,
)

router = APIRouter()
router.include_router(aws_connections.router)
router.include_router(compute_containers.router)
router.include_router(compute_units.router)
router.include_router(compute_policy.router)
router.include_router(compute_machines.router)
router.include_router(compute_workers.router)

__all__ = ["router"]
