from .builds import builds_router
from .members import members_router
from .root import workspaces_router

workspaces_router.include_router(builds_router)
workspaces_router.include_router(members_router)

__all__ = ["workspaces_router"]
