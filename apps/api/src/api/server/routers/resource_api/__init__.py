from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.resource_api import (
    billing,
    compute,
    deployments,
    observability,
    operations,
    secrets,
    tasks,
)

router = APIRouter()
router.include_router(billing.router)
router.include_router(deployments.router)
router.include_router(tasks.router)
router.include_router(secrets.router)
router.include_router(observability.router)
router.include_router(compute.router)
router.include_router(operations.router)

__all__ = ["router"]
