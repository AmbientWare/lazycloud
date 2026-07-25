from __future__ import annotations

DEFAULT_IMAGE_BASE = "python:3.12-slim"
DOCKER_HUB_REGISTRY = "docker.io"
DEFAULT_CONTEXT_IGNORES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
