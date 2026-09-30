from __future__ import annotations

IMAGE_ARCHIVE_KEY_PREFIX = "image-archives"
IMAGE_ARCHIVE_UPLOAD_TIMEOUT_SECONDS = 120


def image_archive_object_key(image_id: str, *, container_id: str, extension: str) -> str:
    """Isolate outstanding upload capabilities belonging to different attempts."""

    if not image_id or not container_id:
        raise ValueError("image archive key requires an image and execution container")
    return f"{IMAGE_ARCHIVE_KEY_PREFIX}/{image_id}/{container_id}.{extension.lstrip('.')}"


DEFAULT_IMAGE_BASE = (
    "docker.io/library/debian:trixie-slim@sha256:"
    "a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a"
)
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
