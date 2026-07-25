from __future__ import annotations

from lazycloud.config import get_profile


def current_workspace(workspace: str | None = None) -> str:
    if workspace:
        return workspace
    return get_profile().workspace


__all__ = ["current_workspace"]
