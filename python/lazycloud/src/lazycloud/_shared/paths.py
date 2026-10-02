from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from lazycloud._shared.app_identity import ENV_PREFIX, HOME_DIR

HOME_ENV = f"{ENV_PREFIX}_HOME"


def state_home(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = source.get(HOME_ENV, f"~/{HOME_DIR}")
    return Path(raw).expanduser().resolve()


__all__ = ["HOME_ENV", "state_home"]
