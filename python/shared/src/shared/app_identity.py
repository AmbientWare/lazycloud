from __future__ import annotations

NAME = "lazycloud"
ENV_PREFIX = NAME.upper().replace("-", "_")
HOME_DIR = f".{NAME}"

SANDBOX_COMPOSE_OVERRIDE_PATH = f"/tmp/{NAME}-docker-compose.override.yml"

__all__ = [
    "ENV_PREFIX",
    "HOME_DIR",
    "NAME",
    "SANDBOX_COMPOSE_OVERRIDE_PATH",
]
