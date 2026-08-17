from __future__ import annotations

from fastapi import FastAPI

from api.server.routers import (
    artifacts,
    collections,
    control_plane,
    endpoints,
    functions,
    gateway,
    images,
    install,
    pods,
    resource_api,
    shells,
    signals,
    system,
    volumes,
    webhooks,
    worker_repository,
)


def include_api_routers(app: FastAPI) -> None:
    app.include_router(system.router)
    app.include_router(install.router)
    app.include_router(webhooks.router)
    app.include_router(resource_api.router)
    app.include_router(control_plane.router)
    app.include_router(volumes.router)
    app.include_router(endpoints.router)
    app.include_router(functions.router)
    app.include_router(gateway.router)
    app.include_router(images.router)
    app.include_router(pods.router)
    app.include_router(artifacts.router)
    app.include_router(shells.router)
    app.include_router(signals.router)
    app.include_router(collections.router)
    app.include_router(worker_repository.router)


__all__ = ["include_api_routers"]
