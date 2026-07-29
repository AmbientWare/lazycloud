from __future__ import annotations

IMAGE_ARCHIVE_KEY_PREFIX = "image-archives"


def image_archive_object_key(image_id: str, *, extension: str) -> str:
    """The one object key for an image's archive.

    Content-addressed by image id alone: no workspace, because the archive is
    global, and no build id, because two builds of the same image produce the same
    bytes and keying by build wrote a full duplicate copy for each.
    """

    if not image_id:
        raise ValueError("image archive key requires an image id")
    return f"{IMAGE_ARCHIVE_KEY_PREFIX}/{image_id}.{extension.lstrip('.')}"


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
