from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.control_plane import (
    apps,
    concurrency,
    custom_domains,
    invitations,
    sessions,
    source_cache_cleanup,
    stubs,
    users,
    workspaces,
)

router = APIRouter()
router.include_router(workspaces.router)
router.include_router(source_cache_cleanup.router)
router.include_router(stubs.router)
router.include_router(apps.router)
router.include_router(concurrency.router)
router.include_router(custom_domains.router)
router.include_router(sessions.router)
router.include_router(users.router)
router.include_router(invitations.router)

__all__ = ["router"]
