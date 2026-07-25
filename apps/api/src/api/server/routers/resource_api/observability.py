from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.resource_api import (
    observability_events,
    observability_logs,
    observability_metrics,
    usage,
)

router = APIRouter()
router.include_router(observability_events.router)
router.include_router(observability_logs.router)
router.include_router(observability_metrics.router)
router.include_router(usage.router)

__all__ = ["router"]
