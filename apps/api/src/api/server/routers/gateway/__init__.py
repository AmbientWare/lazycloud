from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.gateway import (
    agents,
    auth_objects,
    containers_tasks,
    stubs_deployments,
)

router = APIRouter()
router.include_router(auth_objects.router)
router.include_router(containers_tasks.router)
router.include_router(stubs_deployments.router)
router.include_router(agents.router)

__all__ = ["router"]
