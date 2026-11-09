from __future__ import annotations

import os
from collections.abc import Mapping

from lazycloud._shared.env import CONTAINER_ID_ENV, importing_user_code_now


def called_on_import(env: Mapping[str, str] | None = None) -> bool:
    """Whether user code is being imported in this context rather than run."""
    return importing_user_code_now(env)


def is_local(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process runs outside a LazyCloud container."""
    source = env if env is not None else os.environ
    return not source.get(CONTAINER_ID_ENV, "").strip()


__all__ = ["called_on_import", "is_local"]
