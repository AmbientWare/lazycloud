from .registry import registry_router
from .root import workspaces_router
from .usage import usage_router

workspaces_router.include_router(registry_router, prefix="/{workspace_id}")
workspaces_router.include_router(usage_router, prefix="/{workspace_id}")

__all__ = ["workspaces_router"]
