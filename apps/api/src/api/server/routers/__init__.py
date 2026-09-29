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
    ssh,
    system,
    volumes,
    webhooks,
    worker_repository,
)
from api.server.routers.gateway import agents, auth_objects, containers_tasks, stubs_deployments
from api.server.routers.resource_api import tasks


def include_api_routers(app: FastAPI) -> None:
    app.include_router(system.health_router)
    app.include_router(system.router)
    app.include_router(install.router)
    app.include_router(webhooks.router)
    app.include_router(resource_api.router)
    app.include_router(control_plane.router)
    app.include_router(volumes.router)
    app.include_router(endpoints.router)
    app.include_router(functions.router)
    app.include_router(functions.runtime_router)
    app.include_router(gateway.router)
    app.include_router(images.router)
    app.include_router(ssh.router)
    app.include_router(pods.router)
    app.include_router(artifacts.router)
    app.include_router(shells.router)
    app.include_router(collections.router)
    app.include_router(worker_repository.router)
    app.include_router(tasks.router)


def include_management_routers(app: FastAPI) -> None:
    app.include_router(system.health_router)
    app.include_router(system.router)
    app.include_router(install.router)
    app.include_router(webhooks.router)
    app.include_router(resource_api.router)
    app.include_router(control_plane.router)
    app.include_router(images.router)
    app.include_router(auth_objects.router)
    app.include_router(stubs_deployments.router)


def include_execution_routers(app: FastAPI) -> None:
    app.include_router(system.health_router)
    app.include_router(functions.router)
    app.include_router(endpoints.router)
    app.include_router(pods.router)
    app.include_router(shells.router)
    app.include_router(ssh.router)
    app.include_router(containers_tasks.router)
    app.include_router(artifacts.router)
    app.include_router(collections.router)
    app.include_router(volumes.router)
    app.include_router(tasks.router)


def include_runtime_routers(app: FastAPI) -> None:
    app.include_router(system.health_router)
    app.include_router(functions.runtime_router)
    app.include_router(containers_tasks.runtime_router)
    app.include_router(agents.router)
    app.include_router(worker_repository.router)


__all__ = ["include_api_routers"]
