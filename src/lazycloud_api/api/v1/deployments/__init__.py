from .instances import instances_router
from .root import deployments_router
from .secrets import secrets_router
from .services import services_router
from .statuses import statuses_router

# Combine all deployment routes into one router
deployments_router.include_router(statuses_router, prefix="/{deployment_id}")
deployments_router.include_router(instances_router, prefix="/{deployment_id}")
deployments_router.include_router(secrets_router, prefix="/{deployment_id}")
deployments_router.include_router(services_router, prefix="/{deployment_id}")

__all__ = ["deployments_router"]
