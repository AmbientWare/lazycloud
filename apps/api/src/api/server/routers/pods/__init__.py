from __future__ import annotations

from fastapi import APIRouter

from api.server.routers.pods import directories, files, operations, process, proxy

router = APIRouter()
router.include_router(process.router)
router.include_router(files.router)
router.include_router(directories.router)
router.include_router(operations.router)
router.include_router(proxy.router)

__all__ = ["router"]
