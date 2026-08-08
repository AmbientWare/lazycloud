from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.control_plane import (
    apps,
    concurrency,
    custom_domains,
    source_cache_cleanup,
    stubs,
    workspaces,
)

router = APIRouter()
router.include_router(workspaces.router)
router.include_router(source_cache_cleanup.router)
router.include_router(stubs.router)
router.include_router(apps.router)
router.include_router(concurrency.router)
router.include_router(custom_domains.router)

__all__ = ["router"]
