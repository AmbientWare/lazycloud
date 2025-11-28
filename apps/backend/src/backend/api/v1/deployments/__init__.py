from .instances import instances_router
from .root import deployments_router
from .secrets import secrets_router
from .services import services_router
from .statuses import status_router
from .usage import usage_router

# Combine all deployment routes into one router
deployments_router.include_router(status_router)
deployments_router.include_router(instances_router)
deployments_router.include_router(secrets_router)
deployments_router.include_router(services_router)
deployments_router.include_router(usage_router)

__all__ = ["deployments_router"]
