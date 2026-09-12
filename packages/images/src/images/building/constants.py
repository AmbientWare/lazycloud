from __future__ import annotations

DEFAULT_IMAGE_BASE = "python:3.12-slim"
DOCKER_HUB_REGISTRY = "docker.io"
MANAGED_PYTHON_PREFIX = "/opt/runtime-python"
UV_PROJECT_ENVIRONMENT = "/opt/lazycloud/venv"
UV_IMAGE_REFERENCE = (
    "ghcr.io/astral-sh/uv:0.11.29@sha256:"
    "eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc"
)
UV_COPY_INSTRUCTION = f"COPY --from={UV_IMAGE_REFERENCE} /uv /uvx /usr/local/bin/"
BASE_IMAGE_DIGEST_CACHE_TTL_SECONDS = 300
BASE_IMAGE_DIGEST_CACHE_MAX_ENTRIES = 1024
PIP_GROUP_BOUNDARY_FLAGS = frozenset(
    {
        "--no-deps",
        "--only-binary",
        "--no-binary",
        "--prefer-binary",
        "--require-hashes",
        "--pre",
        "--ignore-requires-python",
        "--no-pin",
        "--force-reinstall",
        "--freeze-installed",
        "--update-deps",
        "--no-update-deps",
    }
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
