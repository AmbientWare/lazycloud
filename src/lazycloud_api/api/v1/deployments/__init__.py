"""Deployments API routes."""

from fastapi import APIRouter

from . import crud, services, streaming

DEPLOYMENTS_PREFIX = "/deployments"

# Combine all deployment routes into one router
router = APIRouter(tags=["deployments"])
router.include_router(crud.router, prefix=DEPLOYMENTS_PREFIX)
router.include_router(services.router, prefix=DEPLOYMENTS_PREFIX)
router.include_router(streaming.router, prefix=DEPLOYMENTS_PREFIX)
