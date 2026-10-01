from __future__ import annotations

DEFAULT_IMAGE_BASE = (
    "docker.io/library/debian:trixie-slim@sha256:"
    "a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a"
)
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
